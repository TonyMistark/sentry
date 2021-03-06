from __future__ import absolute_import, print_function

import logging
import posixpath

from sentry.models import Project, EventError
from sentry.plugins import Plugin2
from sentry.lang.native.symbolizer import Symbolizer, have_symsynd
from sentry.models.dsymfile import SDK_MAPPING


logger = logging.getLogger(__name__)

APP_BUNDLE_PATHS = (
    '/var/containers/Bundle/Application/',
    '/private/var/containers/Bundle/Application/',
)

NON_APP_FRAMEWORKS = (
    '/Frameworks/libswiftCore.dylib',
)

SIGNAL_NAMES = {
    1: 'SIGHUP',
    2: 'SIGINT',
    3: 'SIGQUIT',
    4: 'SIGILL',
    5: 'SIGTRAP',
    6: 'SIGABRT',
    7: 'SIGEMT',
    8: 'SIGFPE',
    9: 'SIGKILL',
    10: 'SIGBUS',
    11: 'SIGSEGV',
    12: 'SIGSYS',
    13: 'SIGPIPE',
    14: 'SIGALRM',
    15: 'SIGTERM',
    16: 'SIGURG',
    17: 'SIGSTOP',
    18: 'SIGTSTP',
    19: 'SIGCONT',
    20: 'SIGCHLD',
    21: 'SIGTTIN',
    22: 'SIGTTOU',
    24: 'SIGXCPU',
    25: 'SIGXFSZ',
    26: 'SIGVTALRM',
    27: 'SIGPROF',
    28: 'SIGWINCH',
    29: 'SIGINFO',
    31: 'SIGUSR2',
}


def append_error(data, err):
    data.setdefault('errors', []).append(err)


def process_posix_signal(data):
    signal = data.get('signal', -1)
    signal_name = data.get('name')
    if signal_name is None:
        signal_name = SIGNAL_NAMES.get(signal)
    return {
        'signal': signal,
        'name': signal_name,
        'code': data.get('code'),
        'code_name': data.get('code_name'),
    }


def exception_from_apple_error_or_diagnosis(error, diagnosis=None):
    rv = {}
    error = error or {}

    mechanism = {}
    if 'mach' in error:
        mechanism['mach_exception'] = error['mach']
    if 'signal' in error:
        mechanism['posix_signal'] = process_posix_signal(error['signal'])
    if mechanism:
        mechanism.setdefault('type', 'cocoa')
        rv['mechanism'] = mechanism

    # Start by getting the error from nsexception
    if error:
        nsexception = error.get('nsexception')
        if nsexception:
            rv['type'] = nsexception['name']
            if 'value' in nsexception:
                rv['value'] = nsexception['value']

    # If we don't have an error yet, try to build one from reason and
    # diagnosis
    if 'value' not in rv:
        if 'reason' in error:
            rv['value'] = error['reason']
        elif 'diagnosis' in error:
            rv['value'] = error['diagnosis']
        elif 'mach_exception' in mechanism:
            rv['value'] = mechanism['mach_exception']['exception_name']
        elif 'posix_signal' in mechanism:
            rv['value'] = mechanism['posix_signal']['name']
        else:
            rv['value'] = 'Unknown'

    # Figure out a reasonable type
    if 'type' not in rv:
        if 'mach_exception' in mechanism:
            rv['type'] = 'MachException'
        elif 'posix_signal' in mechanism:
            rv['type'] = 'Signal'
        else:
            rv['type'] = 'Unknown'

    if rv:
        return rv


def is_in_app(frame, app_uuid=None):
    if app_uuid is not None:
        frame_uuid = frame.get('uuid')
        if frame_uuid == app_uuid:
            return True
    object_name = frame.get('object_name', '')
    if not object_name.startswith(APP_BUNDLE_PATHS):
        return False
    if object_name.endswith(NON_APP_FRAMEWORKS):
        return False
    return True


def inject_apple_backtrace(data, frames, diagnosis=None, error=None,
                           system=None, notable_addresses=None):
    # TODO:
    #   user report stacktraces from unity

    app_uuid = None
    if system:
        app_uuid = system.get('app_uuid')
        if app_uuid is not None:
            app_uuid = app_uuid.lower()

    converted_frames = []
    longest_addr = 0
    for frame in reversed(frames):
        fn = frame.get('filename')

        # We only record the offset if we found a symbol but we did not
        # find a line number.  In that case it's the offset in bytes from
        # the beginning of the symbol.
        function = frame['symbol_name'] or '<unknown>'
        lineno = frame.get('line')
        offset = None
        if not lineno:
            offset = frame['instruction_addr'] - frame['symbol_addr']

        cframe = {
            'in_app': is_in_app(frame, app_uuid),
            'abs_path': fn,
            'filename': fn and posixpath.basename(fn) or None,
            # This can come back as `None` from the symbolizer, in which
            # case we need to fill something else in or we will fail
            # later fulfill the interface requirements which say that a
            # function needs to be provided.
            'function': function,
            'package': frame['object_name'],
            'symbol_addr': '%x' % frame['symbol_addr'],
            'instruction_addr': '%x' % frame['instruction_addr'],
            'instruction_offset': offset,
            'lineno': lineno,
        }
        converted_frames.append(cframe)
        longest_addr = max(longest_addr, len(cframe['symbol_addr']),
                           len(cframe['instruction_addr']))

    # Pad out addresses to be of the same length and add prefix
    for frame in converted_frames:
        for key in 'symbol_addr', 'instruction_addr':
            frame[key] = '0x' + frame[key][2:].rjust(longest_addr, '0')

    if converted_frames and notable_addresses:
        converted_frames[-1]['vars'] = notable_addresses

    stacktrace = {'frames': converted_frames}

    if error or diagnosis:
        error = error or {}
        exc = exception_from_apple_error_or_diagnosis(error, diagnosis)
        if exc is not None:
            exc['stacktrace'] = stacktrace
            data['sentry.interfaces.Exception'] = {'values': [exc]}
            # Since we inject the exception late we need to make sure that
            # we set the event type to error as it would be set to
            # 'default' otherwise.
            data['type'] = 'error'
            return

    data['sentry.interfaces.Stacktrace'] = stacktrace


def inject_apple_device_data(data, system):
    container = data.setdefault('device', {})
    try:
        container['name'] = SDK_MAPPING[system['system_name']]
    except LookupError:
        container['name'] = system.get('system_name') or 'Generic Apple'

    if 'system_version' in system:
        container['version'] = system['system_version']
    if 'os_version' in system:
        container['build'] = system['os_version']

    extra = container.setdefault('data', {})
    if 'cpu_arch' in system:
        extra['cpu_arch'] = system['cpu_arch']
    if 'model' in system:
        extra['device_model_id'] = system['model']
    if 'machine' in system:
        extra['device_model'] = system['machine']
    if 'kernel_version' in system:
        extra['kernel_version'] = system['kernel_version']


def record_no_symsynd(data):
    if data.get('sentry.interfaces.AppleCrashReport'):
        append_error(data, {
            'type': EventError.NATIVE_NO_SYMSYND,
        })
        return data


def preprocess_apple_crash_event(data):
    crash_report = data.get('sentry.interfaces.AppleCrashReport')
    if crash_report is None:
        return

    project = Project.objects.get_from_cache(
        id=data['project'],
    )

    system = None
    errors = []
    crash = crash_report['crash']
    crashed_thread = None
    for thread in crash['threads']:
        if thread['crashed']:
            crashed_thread = thread
    if crashed_thread is None:
        append_error(data, {
            'type': EventError.NATIVE_NO_CRASHED_THREAD,
        })

    else:
        system = crash_report.get('system')
        try:
            sym = Symbolizer(project, crash_report['binary_images'],
                             threads=[crashed_thread])
            with sym:
                bt, errors = sym.symbolize_backtrace(
                    crashed_thread['backtrace']['contents'], system)
                inject_apple_backtrace(data, bt, crash.get('diagnosis'),
                                       crash.get('error'), system,
                                       crashed_thread.get('notable_addresses'))
        except Exception as e:
            logger.exception('Failed to symbolicate')
            append_error(data, {
                'type': EventError.NATIVE_INTERNAL_FAILURE,
                'error': '%s: %s' % (e.__class__.__name__, str(e)),
            })

    for error in errors:
        append_error(data, error)

    if system:
        inject_apple_device_data(data, system)

    return data


class NativePlugin(Plugin2):
    can_disable = False

    def get_event_preprocessors(self, **kwargs):
        if not have_symsynd:
            return [record_no_symsynd]
        return [preprocess_apple_crash_event]

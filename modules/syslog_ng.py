# -*- coding: utf-8 -*-
'''
Module for getting information about syslog-ng
===============================================

:maintainer:    Tibor Benke <btibi@sch.bme.hu>
:maturity:      new
:depends:       cmd
:platform:      all

This is module is capable of managing syslog-ng instances which were not
installed via a package manager. Users can use a directory as a parameter
in the case of most functions, which contains the syslog-ng and syslog-ng-ctl
binaries.
'''

from __future__ import generators, with_statement
from time import strftime

import logging
import salt
import cStringIO
import os
import os.path
import salt.utils
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError


__SYSLOG_NG_BINARY_PATH = None
__SYSLOG_NG_CONFIG_FILE = '/etc/syslog-ng.conf'
_STATEMENT_NAMES = ('source', 'destination', 'log', 'parser', 'rewrite',
                    'template', 'channel', 'junction', 'filter', 'options')
__SALT_GENERATED_CONFIG_HEADER = '''#Generated by Salt on {0}'''


class SyslogNgError(Exception):
    pass


log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


def _get_not_None_params(params):
    '''
    Returns the not None elements or params.
    '''
    return filter(lambda x: params[x], params)


def _is_statement_unnamed(statement):
    '''
    Returns True, if the given statement is an unnamed statement, like log or
    junction.

    '''
    return statement in ('log', 'channel', 'junction', 'options')


def _is_statement(name, content):
    '''
    Returns True, if the given name is a statement name and based on the
    content it's a statement.
    '''
    return name in _STATEMENT_NAMES and isinstance(content, list) and \
           (_is_all_element_has_type(content, dict) or
            (len(content) > 1 and _is_all_element_has_type(content[1:], dict) and
             (isinstance(content[0], str) or isinstance(content[0], dict))))


def _is_all_elements_simple_type(container):
    '''
    Returns True, if the given container only has simple types, like int, float, str.
    '''
    return all(map(_is_simple_type, container))


def _is_all_element_has_type(container, type_):
    '''
    Returns True, if all elements in container are instances of the given type.
    '''
    return all(map(lambda x: isinstance(x, type_), container))


def _is_reference(parent, this, state_stack):
    '''
    Returns True, if the parameters are referring to a formerly created
    statement, like: source(s_local);
    '''
    return isinstance(parent, str) and _is_simple_type(this) and state_stack[-1] == 0


def _is_options(parent, this, state_stack):
    '''
    Returns True, if the given parameter this is a list of options.

    '''
    return isinstance(parent, str) and isinstance(this, list) and state_stack[-1] == 0


def _are_parameters(this, state_stack):
    '''
    Returns True, if the given parameter this is a list of parameters.
    '''
    return isinstance(this, list) and state_stack[-1] == 1


def _is_simple_type(value):
    '''
    Returns True, if the given parameter value is an instance of either
    int, str, float or bool.
    '''
    return isinstance(value, str) or isinstance(value, int) or isinstance(value, float) or isinstance(value, bool)


def _is_simple_parameter(this, state_stack):
    '''
    Return True, if the given argument this is a parameter and a simple type.
    '''
    return state_stack[-1] == 2 and (_is_simple_type(this))


def _is_complex_parameter(this, state_stack):
    '''
    Return True, if the given argument this is a parameter and an instance of
    dict.
    '''
    return state_stack[-1] == 2 and isinstance(this, dict)


def _is_list_parameter(this, state_stack):
    '''
    Returns True, if the given argument this is inside a parameter and it's
    type is list.
    '''
    return state_stack[-1] == 3 and isinstance(this, list)


def _is_string_parameter(this, state_stack):
    '''
    Returns True, if the given argument this is inside a parameter and it's
    type is str.
    '''
    return state_stack[-1] == 3 and isinstance(this, str)


def _is_int_parameter(this, state_stack):
    '''
    Returns True, if the given argument this is inside a parameter and it's
    type is int.
    '''
    return state_stack[-1] == 3 and isinstance(this, int)


def _is_boolean_parameter(this, state_stack):
    '''
    Returns True, if the given argument this is inside a parameter and it's
    type is bool.
    '''
    return state_stack[-1] == 3 and isinstance(this, bool)


def _build_statement(id, parent, this, indent, buffer, state_stack):
    '''
    Builds a configuration snippet which represents a statement, like log,
    junction, etc.

    :param id: the name of the statement
    :param parent: the type of the statement
    :param this: the body
    :param indent: indentation before every line
    :param buffer: the configuration is written into this
    :param state_stack: a list, which represents the position in the configuration tree
    '''
    if _is_statement_unnamed(parent) or len(state_stack) > 1:
        buffer.write('{0}{1}'.format(indent, parent) + ' {\n')
    else:
        buffer.write('{0}{1} {2}'.format(indent, parent, id) + ' {\n')
    for i in this:
        if isinstance(i, dict):
            key = i.keys()[0]
            value = i[key]
            state_stack.append(0)
            buffer.write(_build_config(id, key, value, state_stack=state_stack))
            state_stack.pop()
    buffer.write('{0}'.format(indent) + '};')


def _build_complex_parameter(id, this, indent, state_stack):
    '''
    Builds the configuration of a complex parameter (contains more than one item).
    '''
    state_stack.append(3)
    key = this.keys()[0]
    value = this[key]
    begin = '{0}{1}('.format(indent, key)
    content = _build_config(id, key, value, state_stack)
    end = ')'
    state_stack.pop()
    return begin + content + end


def _build_simple_parameter(this, indent):
    '''
    Builds the configuration of a simple parameter.
    '''
    try:
        float(this)
        return indent + this
    except ValueError:
        if isinstance(this, str) and _string_needs_quotation(this):
            return '{0}"{1!s}"'.format(indent, this)
        else:
            return '{0}{1}'.format(indent, this)


def _build_parameters(id, parent, this, buffer, state_stack):
    '''
    Iterates over the list of parameters and builds the configuration.
    '''
    state_stack.append(2)
    params = [_build_config(id, parent, i, state_stack=state_stack) for i in this]
    buffer.write(',\n'.join(params))
    state_stack.pop()


def _build_options(id, parent, this, indent, buffer, state_stack):
    '''
    Builds the options' configuration inside of a statement.
    '''
    state_stack.append(1)
    buffer.write('{0}{1}(\n'.format(indent, parent))
    buffer.write(_build_config(id, parent, this, state_stack=state_stack) + '\n')
    buffer.write(indent + ');\n')
    state_stack.pop()


def _string_needs_quotation(string):
    '''
    Return True, if the given parameter string has special characters, so it
    needs quotation.
    '''
    need_quotation_chars = '$@:/.'

    for i in need_quotation_chars:
        if i in string:
            return True
    return False


def _build_string_parameter(this):
    '''
    Builds the config of a simple string parameter.
    '''
    if _string_needs_quotation(this):
        return '"{0}"'.format(this)
    else:
        return this


def _build_config(salt_id, parent, this, state_stack):
    '''
    Builds syslog-ng configuration from a parsed YAML document. It maintains
    a state_stack list, which represents the current position in the
    configuration tree.

    The last value in the state_stack means:
        0: in the root or in a statement
        1: in an option
        2: in a parameter
        3: in a parameter of a parameter

    Returns the built config.
    '''
    buf = cStringIO.StringIO()

    deepness = len(state_stack) - 1
    # deepness based indentation
    indent = '{0}'.format(deepness * '   ')

    if _is_statement(parent, this):
        _build_statement(salt_id, parent, this, indent, buf, state_stack)
    elif _is_reference(parent, this, state_stack):
        buf.write('{0}{1}({2});'.format(indent, parent, this))
    elif _is_options(parent, this, state_stack):
        _build_options(salt_id, parent, this, indent, buf, state_stack)
    elif _are_parameters(this, state_stack):
        _build_parameters(salt_id, parent, this, buf, state_stack)
    elif _is_simple_parameter(this, state_stack):
        return _build_simple_parameter(this, indent)
    elif _is_complex_parameter(this, state_stack):
        return _build_complex_parameter(salt_id, this, indent, state_stack)
    elif _is_list_parameter(this, state_stack):
        return ', '.join(this)
    elif _is_string_parameter(this, state_stack):
        return _build_string_parameter(this)
    elif _is_int_parameter(this, state_stack):
        return str(this)
    elif _is_boolean_parameter(this, state_stack):
        return 'no' if this else 'yes'
    else:
        # It's an unhandled case
        buf.write('{0}# BUG, please report to the syslog-ng mailing list: syslog-ng@lists.balabit.hu'.format(indent))
        raise SyslogNgError('Unhandled case while generating configuration from YAML to syslog-ng format')

    buf.seek(0)
    return buf.read()


def config(name,
           config,
           write=True):
    '''
    Builds syslog-ng configuration.

    name : the id of the Salt document
    config : the parsed YAML code
    write : if True, it writes  the config into the configuration file,
    otherwise just returns it
    '''
    if not isinstance(config, dict):
        log.debug('Config is: ' + str(config))
        raise SaltInvocationError('The config parameter must be a dictionary')

    statement = config.keys()[0]

    stack = [0]
    configs = _build_config(name, parent=statement, this=config[statement], state_stack=stack)

    succ = write
    if write:
        succ = _write_config(config=configs)

    return _format_state_result(name, result=succ, changes={'new': configs, 'old': ''})


def set_binary_path(name):
    '''
    Sets the path, where the syslog-ng binary can be found.

    If syslog-ng is installed via a package manager, users don't need to use
    this function.
    '''
    global __SYSLOG_NG_BINARY_PATH
    __SYSLOG_NG_BINARY_PATH = name
    return _format_state_result(name, result=True)


def set_config_file(name):
    '''
    Sets the configuration's name.
    '''
    global __SYSLOG_NG_CONFIG_FILE
    old = __SYSLOG_NG_CONFIG_FILE
    __SYSLOG_NG_CONFIG_FILE = name
    return _format_state_result(name, result=True, changes={'new': name, 'old': old})


def get_config_file():
    '''
    Returns the configuration directory, which contains syslog-ng.conf.
    '''
    return __SYSLOG_NG_CONFIG_FILE


def _run_command(cmd, options=()):
    '''
    Runs the command cmd with options as its CLI parameters and returns the result
    as a dictionary.
    '''
    cmd_with_params = [cmd]
    cmd_with_params.extend(options)

    cmd_to_run = " ".join(cmd_with_params)

    try:
        return __salt__['cmd.run_all'](cmd_to_run)
    except Exception as err:
        log.error(str(err))
        raise CommandExecutionError("Unable to run command: " + str(type(err)))


def _add_to_path_envvar(directory):
    '''
    Adds directory to the PATH environment variable and returns the original
    one.
    '''
    orig_path = os.environ["PATH"]
    if directory:
        if not os.path.isdir(directory):
            log.error("The given parameter is not a directory")

        os.environ["PATH"] = "{0}{1}{2}".format(orig_path, os.pathsep, directory)

    return orig_path


def _restore_path_envvar(original):
    '''
    Sets the PATH environment variable to the parameter.
    '''
    if original:
        os.environ["PATH"] = original


def _run_command_in_extended_path(syslog_ng_sbin_dir, command, params):
    '''
    Runs the given command in an environment, where the syslog_ng_sbin_dir is
    added then removed from the PATH.
    '''
    orig_path = _add_to_path_envvar(syslog_ng_sbin_dir)

    if not salt.utils.which(command):
        error_message = "Unable to execute the command '{0}'. It is not in the PATH.".format(command)
        log.error(error_message)
        _restore_path_envvar(orig_path)
        raise CommandExecutionError(error_message)

    ret = _run_command(command, options=params)
    _restore_path_envvar(orig_path)
    return ret


def _format_return_data(retcode, stdout=None, stderr=None):
    '''
    Creates a dictionary from the parameters, which can be used to return data
    to Salt.
    '''
    ret = {"retcode": retcode}
    if stdout is not None:
        ret["stdout"] = stdout
    if stderr is not None:
        ret["stderr"] = stderr
    return ret


def config_test(syslog_ng_sbin_dir=None, cfgfile=None):
    '''
    Runs syntax check against cfgfile. If syslog_ng_sbin_dir is specified, it
    is added to the PATH during the test.

    CLI Example:

    .. code-block:: bash

        salt '*' syslog_ng.config_test
        salt '*' syslog_ng.config_test /home/user/install/syslog-ng/sbin
        salt '*' syslog_ng.config_test /home/user/install/syslog-ng/sbin /etc/syslog-ng/syslog-ng.conf
    '''
    params = ["--syntax-only", ]
    if cfgfile:
        params.append("--cfgfile={0}".format(cfgfile))

    try:
        ret = _run_command_in_extended_path(syslog_ng_sbin_dir, "syslog-ng", params)
    except CommandExecutionError as err:
        return _format_return_data(retcode=-1, stderr=str(err))

    retcode = ret.get("retcode", -1)
    stderr = ret.get("stderr", None)
    stdout = ret.get("stdout", None)
    return _format_return_data(retcode, stdout, stderr)


def version(syslog_ng_sbin_dir=None):
    '''
    Returns the version of the installed syslog-ng. If syslog_ng_sbin_dir is specified, it
    is added to the PATH during the execution of the command syslog-ng.

    CLI Example:

    .. code-block:: bash

        salt '*' syslog_ng.version
        salt '*' syslog_ng.version /home/user/install/syslog-ng/sbin
    '''
    try:
        ret = _run_command_in_extended_path(syslog_ng_sbin_dir, "syslog-ng", ("-V",))
    except CommandExecutionError as err:
        return _format_return_data(retcode=-1, stderr=str(err))

    if ret["retcode"] != 0:
        return _format_return_data(ret["retcode"], stderr=ret["stderr"], stdout=ret["stdout"])

    lines = ret["stdout"].split("\n")
    # The format of the first line in the output is:
    # syslog-ng 3.6.0alpha0
    version_line_index = 0
    version_column_index = 1
    v = lines[version_line_index].split()[version_column_index]
    return _format_return_data(0, stdout=v)


def modules(syslog_ng_sbin_dir=None):
    '''
    Returns the available modules. If syslog_ng_sbin_dir is specified, it
    is added to the PATH during the execution of the command syslog-ng.

    CLI Example:

    .. code-block:: bash

        salt '*' syslog_ng.modules
        salt '*' syslog_ng.modules /home/user/install/syslog-ng/sbin
    '''
    try:
        ret = _run_command_in_extended_path(syslog_ng_sbin_dir, "syslog-ng", ("-V",))
    except CommandExecutionError as err:
        return _format_return_data(retcode=-1, stderr=str(err))

    if ret["retcode"] != 0:
        return _format_return_data(ret["retcode"], ret.get("stdout", None), ret.get("stderr", None))

    lines = ret["stdout"].split("\n")
    for i, line in enumerate(lines):
        if line.startswith("Available-Modules"):
            label, installed_modules = line.split()
            return _format_return_data(ret["retcode"], stdout=installed_modules)
    return _format_return_data(-1, stderr="Unable to find the modules.")


def stats(syslog_ng_sbin_dir=None):
    '''
    Returns statistics from the running syslog-ng instance. If syslog_ng_sbin_dir is specified, it
    is added to the PATH during the execution of the command syslog-ng-ctl.

    CLI Example:

    .. code-block:: bash

        salt '*' syslog_ng.stats
        salt '*' syslog_ng.stats /home/user/install/syslog-ng/sbin
    '''
    try:
        ret = _run_command_in_extended_path(syslog_ng_sbin_dir, "syslog-ng-ctl", ("stats",))
    except CommandExecutionError as err:
        return _format_return_data(retcode=-1, stderr=str(err))

    return _format_return_data(ret["retcode"], ret.get("stdout", None), ret.get("stderr", None))


def _format_state_result(name, result, changes=None, comment=''):
    '''
    Creates the state result dictionary.
    '''
    if changes is None:
        changes = {'old': '', 'new': ''}
    return {'name': name, 'result': result, 'changes': changes, 'comment': comment}


def _add_cli_param(params, key, value):
    '''
    Adds key and value as a command line parameter to params.
    '''
    if value is not None:
        params.append('--{0}={1}'.format(key, value))


def _add_boolean_cli_param(params, key, value):
    '''
    Adds key as a command line parameter to params.
    '''
    if value is True:
        params.append('--{0}'.format(key))


def stop(name=None):
    '''
    Kills syslog-ng.
    '''
    pids = __salt__['ps.pgrep'](pattern='syslog-ng')

    if pids is None or len(pids) == 0:
        return _format_state_result(name,
                                    result=False,
                                    comment='Syslog-ng is not running')

    res = __salt__['ps.pkill']('syslog-ng')
    killed_pids = res['killed']

    if killed_pids == pids:
        changes = {'old': killed_pids, 'new': []}
        return _format_state_result(name, result=True, changes=changes)
    else:
        return _format_state_result(name, result=False)


def start(name=None,
          user=None,
          group=None,
          chroot=None,
          caps=None,
          no_caps=False,
          pidfile=None,
          enable_core=False,
          fd_limit=None,
          verbose=False,
          debug=False,
          trace=False,
          yydebug=False,
          persist_file=None,
          control=None,
          worker_threads=None):
    '''
    Ensures, that syslog-ng is started via the given parameters.

    Users shouldn't use this function, if the service module is available on
    their system.
    '''
    params = []
    _add_cli_param(params, 'user', user)
    _add_cli_param(params, 'group', group)
    _add_cli_param(params, 'chroot', chroot)
    _add_cli_param(params, 'caps', caps)
    _add_boolean_cli_param(params, 'no-capse', no_caps)
    _add_cli_param(params, 'pidfile', pidfile)
    _add_boolean_cli_param(params, 'enable-core', enable_core)
    _add_cli_param(params, 'fd-limit', fd_limit)
    _add_boolean_cli_param(params, 'verbose', verbose)
    _add_boolean_cli_param(params, 'debug', debug)
    _add_boolean_cli_param(params, 'trace', trace)
    _add_boolean_cli_param(params, 'yydebug', yydebug)
    _add_cli_param(params, 'cfgfile', __SYSLOG_NG_CONFIG_FILE)
    _add_boolean_cli_param(params, 'persist-file', persist_file)
    _add_cli_param(params, 'control', control)
    _add_cli_param(params, 'worker-threads', worker_threads)
    cli_params = ' '.join(params)
    if __SYSLOG_NG_BINARY_PATH:
        syslog_ng_binary = os.path.join(__SYSLOG_NG_BINARY_PATH, 'syslog-ng')
        command = syslog_ng_binary + ' ' + cli_params
        result = __salt__['cmd.run_all'](command)
    else:
        command = 'syslog-ng ' + cli_params
        result = __salt__['cmd.run_all'](command)

    if result['pid'] > 0:
        succ = True
    else:
        succ = False

    return _format_state_result(
        name, result=succ, changes={'new': command, 'old': ''}
    )


def reload(name):
    '''
    Reloads syslog-ng.
    '''
    if __SYSLOG_NG_BINARY_PATH:
        syslog_ng_ctl_binary = os.path.join(__SYSLOG_NG_BINARY_PATH, 'syslog-ng-ctl')
        command = syslog_ng_ctl_binary + ' reload'
        result = __salt__['cmd.run_all'](command)
    else:
        command = 'syslog-ng-ctl reload'
        result = __salt__['cmd.run_all'](command)

    succ = True if result['retcode'] == 0 else False
    return _format_state_result(name, result=succ, comment=result['stdout'])


def _format_generated_config_header():
    '''
    Formats a header, which is prepended to all appended config.
    '''
    now = strftime('%Y-%m-%d %H:%M:%S')
    return __SALT_GENERATED_CONFIG_HEADER.format(now)


def write_config(name, config, newlines=2):
    '''
    Writes the given parameter config into the config file.
    '''
    succ = _write_config(config, newlines)
    return _format_state_result(name, result=succ)


def _write_config(config, newlines=2):
    '''
    Writes the given parameter config into the config file.
    '''
    text = config
    if isinstance(config, dict) and len(config.keys()) == 1:
        key = config.keys()[0]
        text = config[key]

    try:
        open_flags = 'a'

        with open(__SYSLOG_NG_CONFIG_FILE, open_flags) as f:
            f.write(text)

            for i in range(0, newlines):
                f.write(os.linesep)

        return True
    except Exception as err:
        log.error(str(err))
        return False


def write_version(name):
    '''
    Removes the previous configuration file, then creates a new one and writes the name line.
    '''
    line = '@version: {0}'.format(name)
    try:
        if os.path.exists(__SYSLOG_NG_CONFIG_FILE):
            log.debug(
                'Removing previous configuration file: {0}'
                .format(__SYSLOG_NG_CONFIG_FILE)
            )
            os.remove(__SYSLOG_NG_CONFIG_FILE)
            log.debug('Configuration file successfully removed')

        header = _format_generated_config_header()
        _write_config(config=header, newlines=1)
        _write_config(config=line, newlines=2)

        return _format_state_result(name, result=True)
    except os.error as err:
        log.error(
            'Failed to remove previous configuration file {0!r} because: {1}'
            .format(__SYSLOG_NG_CONFIG_FILE, str(err))
        )
        return _format_state_result(name, result=False)
#!/usr/bin/env python
from __future__ import print_function
import copy
import functools
from glob import glob
import importlib
import logging
import os
import re
import sqlite3
import sys
import time
import traceback
import json

from slackrtm import SlackClient
from slackrtm.server import SlackConnectionError, SlackLoginError
from .server import LimboServer
from .fakeserver import FakeServer

CURDIR = os.path.abspath(os.path.dirname(__file__))
DIR = functools.partial(os.path.join, CURDIR)

PYTHON3 = sys.version_info[0] > 2

logger = logging.getLogger(__name__)

class InvalidPluginDir(Exception):
    def __init__(self, plugindir):
        message = "Unable to find plugin dir {0}".format(plugindir)
        super(InvalidPluginDir, self).__init__(message)

def init_log(config):
    loglevel = config.get("loglevel", logging.INFO)
    logformat = config.get("logformat", '%(asctime)s:%(levelname)s:%(name)s:%(message)s')
    if config.get("logfile"):
        logging.basicConfig(filename=config.get("logfile"), format=logformat, level=loglevel)
    else:
        logging.basicConfig(format=logformat, level=loglevel)

def strip_extension(lst):
    return (os.path.splitext(l)[0] for l in lst)

def init_plugins(plugindir, plugins_to_load=None):
    if plugindir and not os.path.isdir(plugindir):
        raise InvalidPluginDir(plugindir)

    if not plugindir:
        plugindir = DIR("plugins")

    logger.debug("plugindir: {0}".format(plugindir))

    if os.path.isdir(plugindir):
        pluginfiles = glob(os.path.join(plugindir, "[!_]*.py"))
        plugins = strip_extension(os.path.basename(p) for p in pluginfiles)
    else:
        # we might be in an egg; try to get the files that way
        logger.debug("trying pkg_resources")
        import pkg_resources
        try:
            plugins = strip_extension(
                    pkg_resources.resource_listdir(__name__, "plugins"))
        except OSError:
            raise InvalidPluginDir(plugindir)

    hooks = {}

    oldpath = copy.deepcopy(sys.path)
    sys.path.insert(0, plugindir)

    for plugin in plugins:
        if plugins_to_load and plugin not in plugins_to_load:
            logger.debug("skipping plugin {0}, not in plugins_to_load {1}".format(plugin, plugins_to_load))
            continue

        logger.debug("plugin: {0}".format(plugin))
        try:
            mod = importlib.import_module(plugin)
            modname = mod.__name__
            for hook in re.findall("on_(\w+)", " ".join(dir(mod))):
                hookfun = getattr(mod, "on_" + hook)
                logger.debug("plugin: attaching %s hook for %s", hook, modname)
                hooks.setdefault(hook, []).append(hookfun)

            if mod.__doc__:
                # firstline = mod.__doc__.split('\n')[0]
                part_attachment = json.loads(mod.__doc__)
                hooks.setdefault('help', {})[modname] = part_attachment
                hooks.setdefault('extendedhelp', {})[modname] = mod.__doc__

        # bare except, because the modules could raise any number of errors
        # on import, and we want them not to kill our server
        except:
            logger.warning("import failed on module {0}, module not loaded".format(plugin))
            logger.warning("{0}".format(sys.exc_info()[0]))
            logger.warning("{0}".format(traceback.format_exc()))

    sys.path = oldpath
    return hooks

def run_hook(hooks, hook, *args):
    responses = []
    for hook in hooks.get(hook, []):
        try:
            h = hook(*args)
            if h:
                responses.append(h)
        except:
            logger.warning("Failed to run plugin {0}, module not loaded".format(hook))
            logger.warning("{0}".format(sys.exc_info()[0]))
            logger.warning("{0}".format(traceback.format_exc()))
    return responses

def handle_bot_message(event, server):
    try:
        bot = server.slack.server.bots[event["bot_id"]]
    except KeyError:
        logger.debug("bot_message event {0} has no bot".format(event))
        return

    return "\n".join(run_hook(server.hooks, "bot_message", event, server))

def handle_message(event, server):
    subtype = event.get("subtype", "")
    if subtype == "message_changed":
        return

    if subtype == "bot_message":
        return handle_bot_message(event, server)

    try:
        msguser = server.slack.server.users[event["user"]]
    except KeyError:
        logger.debug("event {0} has no user".format(event))
        return

    return "\n".join(run_hook(server.hooks, "message", event, server))


def handle_channel_joined(event, server):
    temp = "".join(run_hook(server.hooks, "channel_joined", event, server))
    return temp


event_handlers = {
    'message': handle_message,
    'channel_joined': handle_channel_joined
}

def handle_event(event, server):
    handler = event_handlers.get(event.get("type"))
    if handler:
        return handler(event, server)

def getif(config, name, envvar):
    if envvar in os.environ:
        config[name] = os.environ.get(envvar)

def init_config():
    config = {}
    getif(config, "token", "SLACK_TOKEN")
    getif(config, "loglevel", "LIMBO_LOGLEVEL")
    getif(config, "logfile", "LIMBO_LOGFILE")
    getif(config, "logformat", "LIMBO_LOGFORMAT")
    getif(config, "plugins", "LIMBO_PLUGINS")

    return config

def loop(server, test_loop=None):
    """Run the main loop

    server is a limbo Server object
    test_loop, if present, is a number of times to run the loop
    """
    try:
        loops_without_activity = 0
        while test_loop is None or test_loop > 0:
            start = time.time()
            loops_without_activity += 1

            events = server.slack.rtm_read()
            for event in events:
                loops_without_activity = 0
                logger.debug("got {0}".format(event.get("type", event)))
                response = handle_event(event, server)
                if response:
                    if isinstance(event['channel'], dict):
                        channel_id = event['channel']['id']
                    else:
                        channel_id = event['channel']
                    server.slack.rtm_send_message(channel_id, response)

            # Run the loop hook. This doesn't send messages it receives,
            # because it doesn't know where to send them. Use
            # server.slack.post_message to send messages from a loop hook
            run_hook(server.hooks, "loop", server)

            # The Slack RTM API docs say:
            #
            # > When there is no other activity clients should send a ping
            # > every few seconds
            #
            # So, if we've gone >5 seconds without any activity, send a ping.
            # If the connection has broken, this will reveal it so slack can
            # quit
            if loops_without_activity > 5:
                server.slack.server.ping()
                loops_without_activity = 0

            end = time.time()
            runtime = start - end
            time.sleep(max(1-runtime, 0))

            if test_loop:
                test_loop -= 1
    except KeyboardInterrupt:
        if os.environ.get("LIMBO_DEBUG"):
            import pdb; pdb.set_trace()
        raise

def relevant_environ():
    return dict((key, os.environ[key])
                for key in os.environ
                if key.startswith("SLACK") or key.startswith("LIMBO"))

def init_server(args, config, Server=LimboServer, Client=SlackClient):
    init_log(config)
    logger.debug("config: {0}".format(config))
    db = init_db(args.database_name)

    config_plugins = config.get("plugins")
    plugins_to_load = config_plugins.split(",") if config_plugins else []

    hooks = init_plugins(args.pluginpath, plugins_to_load)
    try:
        slack = Client(config["token"])
    except KeyError:
        logger.error("""Unable to find a slack token. The environment variables
limbo sees are:
{0}

and the current config is:
{1}

Try setting your bot's slack token with:

export SLACK_TOKEN=<your-slack-bot-token>
""".format(relevant_environ(), config))
        raise
    server = Server(slack, config, hooks, db)
    return server

# decode a string. if str is a python 3 string, do nothing.
def decode(str_, codec='utf8'):
    if PYTHON3:
        return str_
    else:
        return str_.decode(codec)

# encode a string. if str is a python 3 string, do nothing.
def encode(str_, codec='utf8'):
    if PYTHON3:
        return str_
    else:
        return str_.encode(codec)

def main(args):
    config = init_config()
    if args.test:
        init_log(config)
        return repl(FakeServer(), args)
    elif args.command is not None:
        init_log(config)
        cmd = decode(args.command)
        print(run_cmd(cmd, FakeServer(), args.hook, args.pluginpath, config.get("plugins")))
        return

    server = init_server(args, config)

    try:
        server.slack.rtm_connect()
        # run init hook. This hook doesn't send messages to the server (ought it?)
        run_hook(server.hooks, "init", server)

        loop(server)
    except SlackConnectionError:
        logger.warn("Unable to connect to Slack. Bad network?")
        raise
    except SlackLoginError:
        logger.warn("Login Failed, invalid token <{0}>?".format(config["token"]))
        raise

# run a command. cmd should be a unicode string (str in python3, unicode in python2).
# returns a string appropriate for printing (str in py2 and py3)
def run_cmd(cmd, server, hook, pluginpath, plugins_to_load):
    server.hooks = init_plugins(pluginpath, plugins_to_load)
    event = {'type': hook, 'text': cmd, "user": "2", 'ts': time.time(), 'team': None, 'channel': 'repl_channel'}
    return encode(handle_event(event, server))

# raw_input in 2.6 is input in python 3. Set `input` to the correct function
try:
    input = raw_input
except NameError:
    pass

def repl(server, args):
    try:
        while 1:
            cmd = decode(input("limbo> "))
            if cmd.lower() == "quit" or cmd.lower() == "exit":
                return

            print(run_cmd(cmd, server, args.hook, args.pluginpath, None))
    except (EOFError, KeyboardInterrupt):
        print()
        pass

def init_db(database_file):
    return sqlite3.connect(database_file)

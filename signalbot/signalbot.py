from gi.repository import GLib
from importlib import import_module
from pathlib import Path
from pydbus import connect, SessionBus, SystemBus
import yaml
from threading import Thread
import signal
from sys import exit
from tempfile import TemporaryDirectory
from stat import S_IEXEC, S_IREAD
from os import chdir
from .plugins.plugin import PluginRouter


class Chat(object):

    def __init__(self, bot, id):
        self._bot = bot
        self.is_group = isinstance(id, tuple) and id != ()
        self.id = id

        self._plugin_routers = {}

    @staticmethod
    def get_chat_id_from_sender_and_group_id(sender, group_id):
        if group_id != []:
            # Ensure we have a hashable id
            return tuple(group_id)
        else:
            return sender

    def __str__(self):
        return str(self.id)

    def enable_plugin(self, plugin, plugin_router):
        if plugin not in self._plugin_routers:
            self._plugin_routers[plugin] = plugin_router
            self._plugin_routers[plugin].enable(self)

    def disable_plugin(self, plugin):
        if plugin in self._plugin_routers:
            self._plugin_routers[plugin].disable(self)
            del self._plugin_routers[plugin]

    def triagemessage(self, message):
        for _, plugin_router in self._plugin_routers.items():
            plugin_router.triagemessage(message)

    def reply(self, text, attachments=[]):
        self._bot.send_message(text, attachments, self)

    def error(self, text, attachments=[]):
        self._bot.send_error(text, attachments, self)

    def success(self, text, attachments=[]):
        self._bot.send_success(text, attachments, self)


class Message(object):

    def __init__(self, timestamp, chat, sender, text, attachmentfiles):
        self.timestamp = timestamp
        self.chat = chat
        self.sender = sender
        self.text = text
        self.attachmentfiles = attachmentfiles


class Signalbot(object):

    def __init__(self, data_dir=None, mocker=False):
        self._mocker = mocker

        if data_dir is None:
            self._data_dir = Path.joinpath(Path.home(), '.config', 'signalbot')
        elif type(data_dir) is str:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = data_dir

        self._configfile = Path.joinpath(self._data_dir, 'config.yaml')
        self._config = yaml.load(self._configfile.open('r'))

        defaults = {
            'bus': None,
            'enabled': {},
            'master': None,
            'plugins': [],
            'testing_plugins': [],
        }
        for key, default in defaults.items():
            self._config[key] = self._config.get(key, default)

    def _save_config(self):
        yaml.dump(self._config, self._configfile.open('w'))

    def _get_chat_by_id(self, chat_id):
        if chat_id not in self._chats:
            self._chats[chat_id] = Chat(self, id=chat_id)
        return self._chats[chat_id]

    def _init_plugin(self, plugin, test=False):
        # Load module
        if test:
            module_name = '.tests.plugin_{}'.format(plugin)
        else:
            module_name = '.plugins.{}'.format(plugin)
        module = import_module(module_name, package='signalbot')

        # Initialize plugin router
        if hasattr(module, '__plugin_router__'):
            plugin_router_class = module.__plugin_router__
        else:
            plugin_router_class = PluginRouter
        data_dir = Path.joinpath(self._data_dir, 'plugin-'+plugin)
        plugin_router = plugin_router_class(
            data_dir=data_dir,
            chat_class=module.__plugin_chat__)
        self._plugin_routers[plugin] = plugin_router

        # Enable in configured chats
        for chat_id in self._config['enabled']:
            if plugin in self._config['enabled'][chat_id]:
                chat = self._get_chat_by_id(chat_id)
                chat.enable_plugin(plugin, plugin_router)

    def __enter__(self):

        # SIGTERMs should also lead to __exit__() being called. Note that
        # SIGINTs/KeyboardInterrupts are already handled by GLib.MainLoop
        signal.signal(signal.SIGTERM, self._sigterm_handler)

        if self._config['bus'] == 'session' or self._config['bus'] is None:
            self._bus = SessionBus()
        elif self.args.bus == 'system':
            self._bus = SystemBus()
        else:
            self._bus = connect(self._config['bus'])

        if self._mocker:
            self._signal = self._bus.get('org.signalbot.signalclidbusmock')
        else:
            self._signal = self._bus.get('org.asamk.Signal')
        self._signal.onMessageReceived = self._triagemessage

        # Actively discourage chdir in plugins, see _triagemessage
        self._fakecwd = TemporaryDirectory()
        Path(self._fakecwd.name).chmod(S_IEXEC)
        chdir(self._fakecwd.name)

        try:
            self._plugin_routers = {}
            self._chats = {}
            for plugin in self._config['plugins']:
                self._init_plugin(plugin)
            for plugin in self._config['testing_plugins']:
                self._init_plugin(plugin, test=True)

            self._loop = GLib.MainLoop()
            self._thread = Thread(daemon=True, target=self._loop.run)
            self._thread.start()
        except Exception as e:
            # Try not to leave empty temporary directories behind when e.g. a
            # plugin fails to load
            Path(self._fakecwd.name).chmod(S_IREAD)
            self._fakecwd.cleanup()
            raise e

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._loop.quit()
        self._thread.join()
        self._signal.onMessageReceived = None

        self._plugin_routers = {}
        self._chats = {}

        Path(self._fakecwd.name).chmod(S_IREAD)
        self._fakecwd.cleanup()

    def _sigterm_handler(self, signum, frame):
        # Raises SystemExit exception which then calls __exit__
        exit(0)

    def wait(self):
        self._thread.join()

    def send_message(self, text, attachments, chat):
        if chat.is_group:
            self._signal.sendGroupMessage(text, attachments, list(chat.id))
        else:
            self._signal.sendMessage(text, attachments, [chat.id])

    def send_error(self, text, attachments, chat):
        self.send_message(text + ' ❌', attachments, chat)

    def send_success(self, text, attachments, chat):
        self.send_message(text + ' ✔', attachments, chat)

    def _triagemessage(self,
                       timestamp, sender, group_id, text, attachmentfiles):

        # Don't accumulate Chat instances for chats with no active plugins
        chat_id = Chat.get_chat_id_from_sender_and_group_id(sender, group_id)
        if chat_id in self._chats:
            chat = self._chats[chat_id]
        else:
            chat = Chat(self, chat_id)

        message = Message(timestamp, chat, sender, text, attachmentfiles)

        # Master messages are handled internally and in main thread
        if message.text.startswith('//'):
            self._master_message(message)
            return

        # Other messages are handled by plugins and in separate threads
        chat.triagemessage(message)

        # Check whether we're still in fakecwd
        if Path.cwd() != Path(self._fakecwd.name):
            raise Exception("Do not change the working directory. Use absolute"
                            "paths instead.")

    def _master_print_help(self, message):
        message.chat.reply("""
            Available commands:
            //help
            //enable plugin [plugin ...]
            //disable plugin  [plugin ...]
            //list-enabled
            //list-available
            """)

    def _master_enable(self, message, params):
        for plugin in params:

            if plugin not in self._config['plugins'] + \
                    self._config['testing_plugins']:
                message.chat.error("Plugin {} not loaded".format(plugin))
                continue

            chat_id = message.chat.id
            if chat_id not in self._config['enabled']:
                self._config['enabled'][chat_id] = []

            if plugin in self._config['enabled'][chat_id]:
                message.chat.reply(
                    "Plugin {} is already enabled.".format(plugin))
                continue

            self._config['enabled'][chat_id].append(plugin)
            self._save_config()
            # Use self._get_chat_by_id() to automatically store the chat in
            # self._chats if it hasn't been so far
            chat = self._get_chat_by_id(chat_id)
            chat.enable_plugin(plugin, self._plugin_routers[plugin])
            message.chat.success("Plugin {} enabled.".format(plugin))

    def _master_disable(self, message, params):
        for plugin in params:
            chat_id = message.chat.id

            if chat_id not in self._config['enabled'] or \
                    plugin not in self._config['enabled'][chat_id]:
                message.chat.reply(
                    "Plugin {} is already disabled.".format(plugin))
                continue

            message.chat.disable_plugin(plugin)
            self._config['enabled'][chat_id].remove(plugin)
            if not len(self._config['enabled'][chat_id]):
                del self._config['enabled'][chat_id]
                del self._chats[chat_id]
            self._save_config()
            message.chat.success("Plugin {} disabled.".format(plugin))

    def _master_list_enabled(self, message):
        reply = "Enabled plugins:\n"
        chat_id = message.chat.id
        if chat_id in self._config['enabled']:
            for plugin in self._config['enabled'][chat_id]:
                reply += "{}\n".format(plugin)
        message.chat.reply(reply)

    def _master_list_available(self, message):
        reply = "Available plugins:\n"
        for plugin in self._plugin_routers:
            reply += "{}\n".format(plugin)
        message.chat.reply(reply)

    def _master_message(self, message):
        if message.sender != self._config['master']:
            message.chat.error("You are not my master.")
            return

        params = message.text[2:].split(' ')
        command = params[0]
        params = params[1:]
        if command == "help":
            self._master_print_help(message)
        elif command == "enable":
            self._master_enable(message, params)
        elif command == "disable":
            self._master_disable(message, params)
        elif command == "list-enabled":
            self._master_list_enabled(message)
        elif command == "list-available":
            self._master_list_available(message)
        else:
            message.chat.error("Invalid command.")

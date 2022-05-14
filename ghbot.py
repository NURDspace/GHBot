#! /usr/bin/python3

from enum import Enum
import math
import MySQLdb
import paho.mqtt.client as mqtt
import select
import socket
import sys
import threading
import time
import traceback


class irc(threading.Thread):
    class session_state(Enum):
        DISCONNECTED   = 0x00  # setup socket, connect to host
        CONNECTED_NICK = 0x02  # send NICK
        CONNECTED_USER = 0x03  # send USER
        USER_WAIT      = 0x08  # wait for USER ack
        CONNECTED_JOIN = 0x10  # send JOIN
        CONNECTED_WAIT = 0x11  # wait for 'JOIN' indicating that the JOIN succeeded
        RUNNING        = 0xf0  # go
        DISCONNECTING  = 0xff

    class internal_command_rc(Enum):
        HANDLED      = 0x00
        ERROR        = 0x10
        NOT_INTERNAL = 0xff

    state_timeout = 30         # state changes must not take longer than this

    def __init__(self, host, port, nick, channel, m, db, cmd_prefix):
        super().__init__()

        self.cmd_prefix  = cmd_prefix

        self.db          = db

        self.mqtt        = m

        self.plugins     = dict()

        self.hardcoded_plugins = [ 'addacl', 'delacl', 'listacls', 'forget', 'meet', 'commands', 'help', 'more' ]

        self.plugins['addacl']   = ('Add an ACL, format: addacl user|group <user|group> group|cmd <group-name|cmd-name>', 'sysops')
        self.plugins['delacl']   = ('Remove an ACL, format: delacl <user> group|cmd <group-name|cmd-name>', 'sysops')
        self.plugins['listacls'] = ('List all ACLs for a user or group', 'sysops')
        self.plugins['forget']   = ('Forget a person; removes all ACLs for that nick', 'sysops')
        self.plugins['meet']     = ('Use this when a user (nick) has a new hostname', 'sysops')
        self.plugins['commands'] = ('Show list of known commands', None)
        self.plugins['help']     = ('Help for commands, parameter is the command to get help for', None)
        self.plugins['more']     = ('Continue outputting a too long line of text', None)

        self.topic_privmsg  = f'to/irc/{channel[1:]}/privmsg'  # Send reply in channel via PRIVMSG
        self.topic_notice   = f'to/irc/{channel[1:]}/notice'   # Send reply in channel via NOTICE
        self.topic_topic    = f'to/irc/{channel[1:]}/topic'    # Sets TOPIC for channel

        self.topic_register = f'to/bot/register'  # topic where plugins announce themselves

        self.mqtt.subscribe(self.topic_privmsg,  self._recv_msg_cb)
        self.mqtt.subscribe(self.topic_notice,   self._recv_msg_cb)
        self.mqtt.subscribe(self.topic_topic,    self._recv_msg_cb)
        self.mqtt.subscribe(self.topic_register, self._recv_msg_cb)

        self.host        = host
        self.port        = port
        self.nick        = nick
        self.channel     = channel

        self.fd          = None

        self.state       = self.session_state.DISCONNECTED
        self.state_since = time.time()

        self.users       = dict()

        self.cond_352    = threading.Condition()

        self.more        = ''

        self.name = 'GHBot IRC'
        self.start()

        # ask plugins to register themselves so that we know which
        # commands are available (and what they're for etc.)
        self._plugin_command('register')

    def _plugin_command(self, cmd):
        self.mqtt.publish('from/bot/command', cmd)

    def _set_state(self, s):
        print(f'_set_state: state changes from {self.state} to {s}')

        self.state = s

        self.state_since = time.time()

    def get_state(self):
        return self.state

    def _register_plugin(self, msg):
        try:
            elements = msg.split('|')

            cmd       = None
            descr     = ''
            acl_group = None

            for element in elements:
                k, v = element.split('=')

                if k == 'cmd':
                    cmd = v
                
                elif k == 'descr':
                    descr = v

                elif k == 'agrp':
                    acl_group = v

            if cmd != None:
                if not cmd in self.hardcoded_plugins:
                    if not cmd in self.plugins:
                        print(f'_register_plugin: first announcement of {cmd}')

                    self.plugins[cmd] = (descr, acl_group)

                else:
                    print(f'_register_plugin: cannot override "hardcoded" plugin ({cmd})')

            else:
                print(f'_register_plugin: cmd missing in plugin registration')

        except Exception as e:
            print(f'_register_plugin: problem while processing plugin registration "{msg}"')

    def _recv_msg_cb(self, topic, msg):
        print(f'irc::_recv_msg_cb: received "{msg}" for topic {topic}')

        topic = topic[len(self.mqtt.get_topix_prefix()):]

        if msg.find('\n') != -1 or msg.find('\r') != -1:
            print(f'irc::_recv_msg_cb: invalid content to send for {topic}')

            return

        if topic == self.topic_privmsg:
            self.send(f'PRIVMSG {self.channel} :{msg}')

        elif topic == self.topic_notice:
            self.send(f'NOTICE {self.channel} :{msg}')

        elif topic == self.topic_topic:
            self.send(f'TOPIC {self.channel} :{msg}')

        elif topic == self.topic_register:
            self._register_plugin(msg)

        else:
            print(f'irc::_recv_msg_cb: invalid topic {topic}')

            return

    def send(self, s):
        try:
            self.fd.send(f'{s}\r\n'.encode('utf-8'))

            return True

        except Exception as e:
            print(f'irc::send: failed transmitting to IRC server: {e}')

            self.fd.close()

            self._set_state(self.session_state.DISCONNECTED)

        return False

    def send_ok(self, text):
        print(f'OK: {text}')

        # 200 is arbitrary: does the irc server give a hint on this value?
        if len(text) > 200:
            self.more = text[200:]

            n = math.ceil(len(self.more) / 200)

            self.send(f'PRIVMSG {self.channel} :{text[0:200]} ({n} ~more)')

        else:
            self.send(f'PRIVMSG {self.channel} :{text}')

            self.more = ''

    def send_more(self):
        if self.more == '':
            self.send(f'PRIVMSG {self.channel} :No more ~more')

        else:
            current_more = self.more[0:200]

            if len(self.more) > 200:
                self.more = self.more[200:]

            else:
                self.more = ''

            n = math.ceil(len(self.more) / 200)

            self.send(f'PRIVMSG {self.channel} :{current_more} ({n} ~more)')


    def send_error(self, text):
        print(f'ERROR: {text}')

        self.send(f'PRIVMSG {self.channel} :ERROR: {text}')

    def parse_irc_line(self, s):
        # from https://stackoverflow.com/questions/930700/python-parsing-irc-messages

        prefix = ''
        trailing = []

        if s[0] == ':':
            prefix, s = s[1:].split(' ', 1)

        if s.find(' :') != -1:
            s, trailing = s.split(' :', 1)

            args = s.split()
            args.append(trailing)

        else:
            args = s.split()

        command = args.pop(0)

        return prefix, command, args

    def check_acls(self, who, command):
        # "no group" is for everyone
        if command in self.plugins and self.plugins[command][1] == None:
            return True

        self.db.probe()  # to prevent those pesky "sever has gone away" problems

        cursor = self.db.db.cursor()

        # check per user ACLs (can override group as defined in plugin)
        cursor.execute('SELECT COUNT(*) FROM acls WHERE command=%s AND who=%s', (command.lower(), who.lower()))

        row = cursor.fetchone()

        if row[0] >= 1:
            return True

        # check per group ACLs (can override group as defined in plugin)
        cursor.execute('SELECT COUNT(*) FROM acls, acl_groups WHERE acl_groups.who=%s AND acl_groups.group_name=acls.who AND command=%s', (who.lower(), command.lower()))

        row = cursor.fetchone()

        if row[0] >= 1:
            return True

        # check if user is in group as specified by plugin
        cursor.execute('SELECT COUNT(*) FROM acl_groups WHERE group_name=%s AND who=%s', (self.plugins[command][1], who))

        row = cursor.fetchone()

        if row[0] >= 1:
            return True

        return False

    def list_acls(self, who):
        self.db.probe()

        cursor = self.db.db.cursor()

        cursor.execute('SELECT DISTINCT item FROM (SELECT command AS item FROM acls WHERE who=%s UNION SELECT group_name AS item FROM acl_groups WHERE who=%s) AS in_ ORDER BY item', (who.lower(), who.lower()))

        out = []

        for row in cursor:
            out.append(row[0])

        return out

    def add_acl(self, who, command):
        self.db.probe()

        cursor = self.db.db.cursor()

        try:
            cursor.execute('INSERT INTO acls(command, who) VALUES(%s, %s)', (command.lower(), who.lower()))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::add_acl: failed to insert acl ({e})')

        return False

    def del_acl(self, who, command):
        self.db.probe()

        cursor = self.db.db.cursor()

        try:
            cursor.execute('DELETE FROM acls WHERE command=%s AND who=%s LIMIT 1', (command.lower(), who.lower()))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::del_acl: failed to delete acl ({e})')
        
        return False

    def forget_acls(self, who):
        match_ = who + '!%'

        cursor = self.db.db.cursor()

        try:
            cursor.execute('DELETE FROM acls WHERE who LIKE %s', (match_,))

            cursor.execute('DELETE FROM acl_groups WHERE who LIKE %s', (match_,))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::forget_acls: failed to forget acls for {match_}: {e}')
        
        return False

    # new_fullname is the new 'nick!user@host'
    def update_acls(self, who, new_fullname):
        self.db.probe()

        match_ = who + '!%'

        cursor = self.db.db.cursor()

        try:
            cursor.execute('UPDATE acls SET who=%s WHERE who LIKE %s', (new_fullname, match_))

            cursor.execute('UPDATE acl_groups SET who=%s WHERE who LIKE %s', (new_fullname, match_))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::update_acls: failed to update acls ({e})')
        
        return False

    def group_add(self, who, group):
        self.db.probe()

        cursor = self.db.db.cursor()

        try:
            cursor.execute('INSERT INTO acl_groups(who, group_name) VALUES(%s, %s)', (who.lower(), group.lower()))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::group_add: failed to insert group-member ({e})')

        return False

    def group_del(self, who, group):
        self.db.probe()

        cursor = self.db.db.cursor()

        try:
            cursor.execute('DELETE FROM acl_groups WHERE who=%s AND group_name=%s LIMIT 1', (who.lower(), group.lower()))

            self.db.db.commit()

            return True

        except Exception as e:
            self.send_error(f'irc::group-del: failed to delete group-member ({e})')

        return False

    def check_user_known(self, user):
        if '!' in user:
            for cur_user in self.users:
                if self.users[cur_user] == user:
                    return True

            return False

        if not user in self.users:
            return False

        if self.users[user] == None or self.users[user] == '?':
            return False

        return True

    def is_group(self, group):
        self.db.probe()

        cursor = self.db.db.cursor()

        try:
            cursor.execute('SELECT COUNT(*) FROM acl_groups WHERE group_name=%s LIMIT 1', (group.lower(), ))

            row = cursor.fetchone()

            if row[0] >= 1:
                return True

        except Exception as e:
            self.send_error(f'irc::is_group: failed to query database for group {group} ({e})')

        return False

    # e.g. 'group', 'bla' where 'group' is the key and 'bla' the value
    def find_key_in_list(self, list_, item, search_start):
        try:
            idx = list_.index(item, search_start)

            # check if an argument is following it
            if idx == len(list_) - 1:
                idx = None

        except ValueError as ve:
            idx = None

        return idx

    def invoke_who_and_wait(self, user):
        self.send(f'WHO {user}')

        start = time.time()

        while self.check_user_known(user) == False:
            t_diff = time.time() - start

            if t_diff >= 5.0:
                break

            with self.cond_352:
                self.cond_352.wait(5.0 - t_diff)

    def list_plugins(self):
        plugins = ''

        for plugin in self.plugins:
            if plugins != '':
                plugins += ', '

            plugins += plugin

        self.send_ok(f'Known commands: {plugins}')

    def invoke_internal_commands(self, prefix, command, args):
        splitted_args = None

        if len(args) == 2:
            splitted_args = args[1].split(' ')

        identifier  = None

        target_type = None

        check_user  = '(not given)'

        if splitted_args != None and len(splitted_args) >= 2:
            if len(splitted_args) >= 3:  # addacl
                target_type = splitted_args[1]

                check_user = splitted_args[2]

            else:
                target_type = None

                check_user = splitted_args[1]

            if check_user in self.users:
                identifier = self.users[check_user]

            elif '!' in check_user:
                identifier = check_user

            elif self.is_group(check_user):
                identifier = check_user

        identifier_is_known = (self.check_user_known(identifier) or self.is_group(identifier)) if identifier != None else False

        if command == 'addacl':
            group_idx = self.find_key_in_list(splitted_args, 'group', 2)

            cmd_idx   = self.find_key_in_list(splitted_args, 'cmd',   2)

            if not identifier_is_known and target_type == 'user':
                self.invoke_who_and_wait(check_user)

                if check_user in self.users:
                    identifier = self.users[check_user]

            if group_idx != None:
                group_name = splitted_args[group_idx + 1]

                if self.group_add(identifier, group_name):  # who, group
                    self.send_ok(f'User {identifier} added to group {group_name}')

                    return self.internal_command_rc.HANDLED

                else:
                    return self.internal_command_rc.ERROR

            elif cmd_idx != None:
                cmd_name = splitted_args[cmd_idx + 1]

                if cmd_name in self.plugins:
                    if self.add_acl(identifier, cmd_name):  # who, command
                        self.send_ok(f'ACL added for user {identifier} for command {cmd_name}')

                        return self.internal_command_rc.HANDLED

                    else:
                        return self.internal_command_rc.ERROR

                else:
                    self.send_error(f'ACL added for user {identifier} for command {cmd_name} NOT added: command/plugin not known')

                    return self.internal_command_rc.HANDLED

            else:
                self.send_error(f'Usage: addacl user|group <user|group> group|cmd <group-name|cmd-name>')

                return self.internal_command_rc.ERROR

        elif command == 'delacl':
            group_idx = self.find_key_in_list(splitted_args, 'group', 2)

            cmd_idx   = self.find_key_in_list(splitted_args, 'cmd',   2)

            if not identifier_is_known and target_type == 'user':
                self.invoke_who_and_wait(check_user)

                if check_user in self.users:
                    identifier = self.users[check_user]

            if group_idx != None:
                group_name = splitted_args[group_idx + 1]

                if self.group_del(identifier, group_name):  # who, group
                    self.send_ok(f'User {identifier} removed from group {group_name}')

                    return self.internal_command_rc.HANDLED

                else:
                    return self.internal_command_rc.ERROR

            elif cmd_idx != None:
                cmd_name = splitted_args[cmd_idx + 1]

                if self.del_acl(identifier, cmd_name):  # who, command
                    self.send_ok(f'ACL removed for user {identifier} for command {cmd_name}')

                    return self.internal_command_rc.HANDLED

                else:
                    return self.internal_command_rc.ERROR

            else:
                self.send_error(f'Usage: delacl <user> group|cmd <group-name|cmd-name>')

                return self.internal_command_rc.ERROR

        elif command == 'listacls':
            if not identifier_is_known:
                self.invoke_who_and_wait(check_user)

                if check_user in self.users:
                    identifier = self.users[check_user]

            if identifier != None:
                acls = self.list_acls(identifier)

                str_acls = ', '.join(acls)

                self.send_ok(f'ACLs for user {identifier}: "{str_acls}"')

            else:
                self.send_error('Please provide a nick')

            return self.internal_command_rc.HANDLED

        elif command == 'meet':
            if splitted_args != None and len(splitted_args) == 2:
                user_to_update = splitted_args[1]

                self.invoke_who_and_wait(user_to_update)

                if user_to_update in self.users:
                    self.update_acls(user_to_update, self.users[user_to_update])

                    self.send_ok(f'User {user_to_update} updated to {self.users[user_to_update]}')

                else:
                    self.send_error(f'User {user_to_update} is not known')

            else:
                self.send_error(f'Meet parameter missing ({splitted_args} given)')

        elif command == 'commands':
            self.list_plugins()

            return self.internal_command_rc.HANDLED

        elif command == 'help':
            if len(splitted_args) == 2:
                cmd = splitted_args[1]

                if cmd in self.plugins:
                    self.send_ok(f'Command {cmd}: {self.plugins[cmd][0]} (group: {self.plugins[cmd][1]})')

                else:
                    self.send_error(f'Command/plugin not known')

            else:
                self.list_plugins()

            return self.internal_command_rc.HANDLED

        elif command == 'more':
            self.send_more()

            return self.internal_command_rc.HANDLED

        elif command == 'forget':
            if len(splitted_args) == 2:
                user = splitted_args[1]

                if self.forget_acls(user):
                    self.send_ok(f'User {user} forgotten')

                else:
                    self.send_error(f'User {user} not known or some other error')

            else:
                self.send_error(f'User not specified')

            return self.internal_command_rc.HANDLED

        return self.internal_command_rc.NOT_INTERNAL

    def handle_irc_commands(self, prefix, command, args):
        if command != 'PING':
            print(prefix, '|', command, '|', args)

        if len(command) == 3 and command.isnumeric():
            if command == '001':
                if self.state == self.session_state.USER_WAIT:
                    self._set_state(self.session_state.CONNECTED_JOIN)

                else:
                    print(f'irc::run: invalid state for "001" command {self.state}')

                    self._set_state(self.session_state.DISCONNECTING)

            elif command == '352':  # reponse to 'WHO'
                self.users[args[5]] = f'{args[5]}!{args[2]}@{args[3]}'

                print(f'{args[5]} is {self.users[args[5]]}')

            elif command == '353':  # users in the channel
                for user in args[3].split(' '):
                    self.users[user] = '?'

            # 315 is 'end of who'
            if command == '352' or command == '315':
                with self.cond_352:
                    self.cond_352.notify_all()

        elif command == 'JOIN':
            if self.state == self.session_state.CONNECTED_WAIT:
                self._set_state(self.session_state.RUNNING)

            self.users[prefix.split('!')[0]] = prefix.lower()

        elif command == 'PART':
            del self.users[prefix.split('!')[0]]

        elif command == 'KICK':
            del self.users[args[1]]

        elif command == 'NICK':
            old_lower_prefix = prefix.lower()

            excl_mark    = old_lower_prefix.find('!')

            old_user     = old_lower_prefix[0:excl_mark]

            del self.users[old_user]
        
            new_user     = args[0]

            new_prefix   = new_user + old_lower_prefix[excl_mark:]

            self.users[new_user] = new_prefix

            print(f'{old_lower_prefix} => {new_prefix}')

        elif command == 'PING':
            if len(args) >= 1:
                self.send(f'PONG {args[0]}')

            else:
                self.send(f'PONG')

        elif command == 'PRIVMSG':
            if len(args) >= 2 and len(args[1]) >= 2:
                if args[1][0] == self.cmd_prefix:
                    command = args[1][1:].split(' ')[0]

                    if not command in self.plugins:
                        self.send_error(f'Command "{command}" is not known')

                    elif self.check_acls(prefix, command):
                        # returns False when the command is not internal
                        rc = self.invoke_internal_commands(prefix, command, args)

                        if rc == self.internal_command_rc.HANDLED:
                            pass

                        elif rc == self.internal_command_rc.NOT_INTERNAL:
                            self.mqtt.publish(f'from/irc/{args[0][1:]}/{prefix}/{command}', args[1])

                        elif rc == self.internal_command_rc.ERROR:
                            pass

                        else:
                            self.send_error(f'irc::run: unexpected return code from internal commands handler ({rc})')

                    else:
                        self.send_error(f'Command "{command}" denied for user "{prefix}"')

                else:
                    self.mqtt.publish(f'from/irc/{args[0][1:]}/{prefix}/message', args[1])

        elif command == 'NOTICE':
            if len(args) >= 2:
                self.mqtt.publish(f'from/irc/{args[0][1:]}/{prefix}/notice', args[1])

        else:
            print(f'irc::run: command "{command}" is not known (for {prefix})')

    def handle_irc_command_thread_wrapper(self, prefix, command, arguments):
        try:
            self.handle_irc_commands(prefix, command, arguments)

        except Exception as e:
            self.send_error(f'irc::handle_irc_command_thread_wrapper: exception "{e}" during execution of IRC command "{command}"')

            traceback.print_exc(file=sys.stdout)

    def run(self):
        print('irc::run: started')

        buffer = ''

        while True:
            if self.state == self.session_state.DISCONNECTING:
                self.fd.close()

                self._set_state(self.session_state.DISCONNECTED)

            elif self.state == self.session_state.DISCONNECTED:
                self.fd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

                print(f'irc::run: connecting to [{self.host}]:{self.port}')

                try:
                    self.fd.connect((self.host, self.port))

                    self.poller = select.poll()

                    self.poller.register(self.fd, select.POLLIN)

                    self._set_state(self.session_state.CONNECTED_NICK)

                except Exception as e:
                    self.send_error(f'irc::run: failed to connect: {e}')
                    
                    self.fd.close()

            elif self.state == self.session_state.CONNECTED_NICK:
                # apparently only error responses are returned, no acks
                if self.send(f'NICK {self.nick}'):
                    self._set_state(self.session_state.CONNECTED_USER)

            elif self.state == self.session_state.CONNECTED_USER:
                if self.send(f'USER {self.nick} 0 * :{self.nick}'):
                    self._set_state(self.session_state.USER_WAIT)

            elif self.state == self.session_state.CONNECTED_JOIN:
                if self.send(f'JOIN {self.channel}'):
                    self._set_state(self.session_state.CONNECTED_WAIT)

            elif self.state == self.session_state.USER_WAIT:
                # handled elsewhere
                pass

            elif self.state == self.session_state.CONNECTED_WAIT:
                # handled elsewhere
                pass

            elif self.state == self.session_state.RUNNING:
                pass

            else:
                print(f'irc::run: internal error, invalid state {self.state}')

            if self.state != self.session_state.DISCONNECTED and (len(buffer) > 0 or len(self.poller.poll(100)) > 0):
                lf_index = buffer.find('\n')

                if lf_index == -1:
                    try:
                        buffer += self.fd.recv(4096).decode('utf-8')

                    except Exception as e:
                        self.send_error(f'irc::run: cannot decode text from irc-server')

                    lf_index = buffer.find('\n')

                    if lf_index == -1:
                        continue

                line = buffer[0:lf_index].rstrip('\r').strip()
                buffer = buffer[lf_index + 1:]

                prefix, command, arguments = self.parse_irc_line(line)

                t = threading.Thread(target=self.handle_irc_command_thread_wrapper, args=(prefix, command, arguments), daemon=True)
                t.name = 'GHBot input'
                t.start()

            if not self.state in [ self.session_state.DISCONNECTED, self.session_state.DISCONNECTING, self.session_state.RUNNING ]:
                takes = time.time() - self.state_since

                if takes > irc.state_timeout:
                    print(f'irc::run: state {self.state} timeout ({takes} > {irc.state_timeout})')

                    self._set_state(self.session_state.DISCONNECTING)

class mqtt_handler(threading.Thread):
    def __init__(self, broker_ip, topic_prefix):
        super().__init__()

        self.client = mqtt.Client()

        self.topic_prefix = topic_prefix

        self.topics = []

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.client.connect(broker_ip, 1883, 60)

        self.name = 'GHBot MQTT'
        self.start()

    def get_topix_prefix(self):
        return self.topic_prefix

    def subscribe(self, topic, msg_recv_cb):
        print(f'mqtt_handler::topic: subscribe to {self.topic_prefix}{topic}')

        self.topics.append((self.topic_prefix + topic, msg_recv_cb))

        self.client.subscribe(self.topic_prefix + topic)

    def publish(self, topic, content):
        print(f'mqtt_handler::topic: publish "{content}" to "{self.topic_prefix}{topic}"')

        self.client.publish(self.topic_prefix + topic, content)

    def on_connect(self, client, userdata, flags, rc):
        for topic in self.topics:
            print(f'mqtt_handler::topic: re-subscribe to {topic[0]}')

            self.client.subscribe(topic[0])

    def on_message(self, client, userdata, msg):
        print(f'mqtt_handler::topic: received "{msg.payload}" in topic "{msg.topic}"')

        for topic in self.topics:
            if topic[0] == msg.topic:
                topic[1](msg.topic, msg.payload.decode('utf-8'))

                return

        print(f'mqtt_handler::topic: no handler for topic "{msg.topic}"')

    def run(self):
        while True:
            print('mqtt_handler::run: looping')

            self.client.loop_forever()

class dbi(threading.Thread):
    def __init__(self, host, user, password, database):
        super().__init__()

        self.host = host
        self.user = user
        self.password = password
        self.database = database

        self.reconnect()

        self.name = 'GHBot MySQL'
        self.start()

    def reconnect(self):
        self.db = MySQLdb.connect(self.host, self.user, self.password, self.database)

    def probe(self):
        try:
            cursor = self.db.cursor()

            cursor.execute('SELECT NOW()')

            cursor.fetchone()

        except Exception as e:
            print(f'MySQL indicated error: {e}')

            self.reconnect()

    def run(self):
        while True:
            self.probe()

            time.sleep(29)

class irc_keepalive(threading.Thread):
    def __init__(self, i):
        super().__init__()

        self.i = i

        self.name = 'GHBot keepalive'
        self.start()

    def run(self):
        while True:
            try:
                if i.get_state() == irc.session_state.RUNNING:
                    i.send('TIME')

                    time.sleep(30)

                else:
                    time.sleep(5)

            except Exception as e:
                print(f'irc_keepalive::run: exception {e}')

                time.sleep(1)

db = dbi('mauer', 'ghbot', 'ghbot', 'ghbot')

m = mqtt_handler('192.168.64.1', 'GHBot/')

i = irc('192.168.64.1', 6667, 'ghbot', '#test', m, db, '~')

ka = irc_keepalive(i)

print('Go!')

while True:
    time.sleep(1.)

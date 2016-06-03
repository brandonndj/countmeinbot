import webapp2
import logging
import json
import telegram
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from google.appengine.api import taskqueue, memcache
from google.appengine.ext import ndb
from collections import OrderedDict

from secrets import BOT_TOKEN
bot = telegram.Bot(token=BOT_TOKEN)

RECOGNISED_ERRORS = ['Message is not modified']

class User(ndb.Model):
    first_name = ndb.TextProperty()
    last_name = ndb.TextProperty()
    username = ndb.StringProperty(indexed=False)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True, indexed=False)

class Poll(ndb.Model):
    admin_uid = ndb.StringProperty()
    title = ndb.TextProperty()
    active = ndb.BooleanProperty(default=True)
    multi = ndb.BooleanProperty(default=True, indexed=False)

    options = ndb.PickleProperty(repeated=True)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True, indexed=False)

    def generate_options_summary(self):
        output = ''
        for option in self.options:
            output += option.title + ' / '
        return output.rstrip(' / ')

    def render_text(self):
        output = self.title + '\n\n'
        all_uids = []
        for option in self.options:
            output += option.title + '\n'
            output += option.generate_name_list() + '\n\n'
            all_uids += option.people.keys()
        num_respondents = len(set(all_uids))
        if num_respondents == 0:
            respondents_summary = 'Nobody has responded'
        elif num_respondents == 1:
            respondents_summary = '1 person responded'
        else:
            respondents_summary = '{} people responded'.format(num_respondents)
        output += u'\U0001f465' + ' ' + respondents_summary
        return output

    def build_vote_buttons(self):
        poll_id = self.key.id()
        options = self.options
        buttons = []
        for i in range(len(options)):
            data = '{} {}'.format(poll_id, i)
            button = InlineKeyboardButton(options[i].title, callback_data=data)
            buttons.append([button])
        return InlineKeyboardMarkup(buttons).to_dict()

    def build_admin_buttons(self):
        return self.build_vote_buttons()

class Option(object):
    def __init__(self, title, people=OrderedDict()):
        self.title = title
        self.people = people

    def toggle(self, uid, first_name, last_name):
        if self.people.get(uid):
            self.people.pop(uid, None)
            return 'Your name was removed from ' + self.title + '!'
        else:
            self.people[uid] = (first_name, last_name)
            return 'Your name was added to ' + self.title + '!'

    def generate_name_list(self):
        output = ''
        for (first_name, _) in self.people.values():
            output += first_name + '\n'
        return output.strip()

class FrontPage(webapp2.RequestHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('CountMeIn Bot backend running...')

class TelegramHandler(webapp2.RequestHandler):
    def handle_exception(self, exception, debug):
        if isinstance(exception, telegram.error.NetworkError):
            if str(exception) in RECOGNISED_ERRORS:
                logging.info(exception)
            else:
                logging.warning(exception)
                self.abort(500)
        else:
            logging.error(exception)

class SendMessagePage(TelegramHandler):
    def post(self):
        logging.debug(self.request.body)
        kwargs = json.loads(self.request.body)
        bot.sendMessage(**kwargs)
        logging.info('Message sent!')

class EditMessageTextPage(TelegramHandler):
    def post(self):
        logging.debug(self.request.body)
        kwargs = json.loads(self.request.body)
        bot.editMessageText(**kwargs)
        logging.info('Message text edited!')

class EditMessageReplyMarkupPage(TelegramHandler):
    def post(self):
        logging.debug(self.request.body)
        kwargs = json.loads(self.request.body)
        bot.editMessageReplyMarkup(**kwargs)
        logging.info('Message reply markup edited!')

class MainPage(webapp2.RequestHandler):
    NEW_POLL = 'Let\'s create a new poll. First, send me the title.'
    PREMATURE_DONE = 'Sorry, a poll needs to have at least one option to work.'
    FIRST_OPTION = 'New poll: \'{}\'\n\nPlease send me the first answer option.'
    NEXT_OPTION = 'Good. Now send me another answer option, or /done to finish.'
    HELP = 'This bot will help you create polls. Use /start to create a poll here, ' + \
           'then publish it to groups or send it to individual friends.\n\n' + \
           'Send /polls to manage your existing polls.'
    DONE = u'\U0001f44d' + ' Poll created. You can now publish it to a group or send it to ' + \
           'your friends in a private message. To do this, tap the button below or start ' + \
           'your message in any other chat with @countmeinbot and select one of your polls to send.'

    def post(self):
        logging.debug(self.request.body)
        update = telegram.Update.de_json(json.loads(self.request.body))

        if update.message:
            logging.info('Processing incoming message')
            self.handle_message(update.message)
        elif update.callback_query:
            logging.info('Processing incoming callback query')
            self.handle_callback_query(update.callback_query)
        elif update.inline_query:
            logging.info('Processing incoming inline query')
            self.handle_inline_query(update.inline_query)

    def handle_message(self, message):
        u = message.from_user
        update_user(u.id, first_name=u.first_name, last_name=u.last_name, username=u.username)
        uid = str(message.chat.id)

        if not message.text:
            return

        text = message.text.encode('utf-8')
        responding_to = memcache.get(uid)

        if text == '/start':
            send_message(chat_id=uid, text=self.NEW_POLL)
            memcache.set(uid, value='START', time=3600)

        elif text == '/done':
            if responding_to and responding_to.startswith('OPT '):
                poll_id = int(responding_to[4:])
                poll = get_poll(poll_id)
                option_count = len(poll.options)
                if option_count > 0:
                    send_message(chat_id=uid, text=self.DONE)
                    send_poll(uid, poll)
                    memcache.delete(uid)
                else:
                    send_message(chat_id=uid, text=self.PREMATURE_DONE)
            else:
                send_message(chat_id=uid, text=self.HELP)

        else:
            if not responding_to:
                send_message(chat_id=uid, text=self.HELP)

            elif responding_to == 'START':
                new_poll = Poll(admin_uid=uid, title=text)
                poll_key = new_poll.put()
                poll_id = str(poll_key.id())
                send_message(chat_id=uid, text=self.FIRST_OPTION.format(text))
                memcache.set(uid, value='OPT ' + poll_id, time=3600)

            elif responding_to.startswith('OPT '):
                poll_id = int(responding_to[4:])
                poll = get_poll(poll_id)
                poll.options.append(Option(message.text))
                poll.put()
                option_count = len(poll.options)
                if option_count < 10:
                    send_message(chat_id=uid, text=self.NEXT_OPTION)
                else:
                    send_message(chat_id=uid, text=self.DONE)
                    send_poll(uid, poll)
                    memcache.delete(uid)

            else:
                send_message(chat_id=uid, text=self.HELP)
                memcache.delete(uid)

    def handle_callback_query(self, callback_query):
        qid = callback_query.id
        data = callback_query.data

        uid = str(callback_query.from_user.id)
        first_name = callback_query.from_user.first_name
        last_name = callback_query.from_user.last_name

        imid = callback_query.inline_message_id
        if not imid:
            chat_id = callback_query.message.chat.id
            mid = callback_query.message.message_id

        try:
            params = data.split()
            poll_id = int(params[0])
            opt_id = int(params[1])
        except:
            logging.info('Invalid callback query data')
            return

        (poll, status) = toggle_poll(poll_id, opt_id, uid, first_name, last_name)

        if poll:
            updated_text = poll.render_text()
            buttons = poll.build_vote_buttons()

            if imid:
                edit_message_text(inline_message_id=imid, text=updated_text, reply_markup=buttons)
            else:
                edit_message_text(chat_id=chat_id, message_id=mid, text=updated_text,
                                  reply_markup=buttons)

        payload = {'method': 'answerCallbackQuery', 'callback_query_id': qid, 'text': status}
        output = json.dumps(payload)
        self.response.headers['Content-Type'] = 'application/json'
        self.response.write(output)
        logging.info('Answered callback query!')
        logging.debug(output)

    def handle_inline_query(self, inline_query):
        qid = inline_query.id
        # query_text = inline_query.query

        uid = str(inline_query.from_user.id)
        query = Poll.query(Poll.admin_uid == uid).order(-Poll.created)
        results = []
        for poll in query.fetch(50):
            qr_id = str(poll.key.id())
            qr_title = poll.title
            qr_description = poll.generate_options_summary()
            content = {'message_text': poll.render_text()}
            reply_markup = poll.build_vote_buttons()
            result = {'type': 'article', 'id': qr_id, 'title': qr_title,
                      'description': qr_description, 'input_message_content': content,
                      'reply_markup': reply_markup}
            results.append(result)

        payload = {'method': 'answerInlineQuery', 'inline_query_id': qid, 'results': results,
                   'switch_pm_text': 'Create new poll', 'cache_time': 0}
        output = json.dumps(payload)
        self.response.headers['Content-Type'] = 'application/json'
        self.response.write(output)
        logging.info('Answered inline query!')
        logging.debug(output)

@ndb.transactional
def toggle_poll(poll_id, opt_id, uid, first_name, last_name):
    poll = get_poll(poll_id)
    if not poll:
        return (None, 'Sorry, this poll has been deleted')
    elif opt_id >= len(poll.options):
        return (None, 'Sorry, that\'s an invalid option')
    status = poll.options[opt_id].toggle(uid, first_name, last_name)
    poll.put()
    return (poll, status)

def get_poll(pid):
    key = ndb.Key('Poll', pid)
    return key.get()

def send_poll(uid, poll, mode='vote'):
    poll_text = poll.render_text()
    poll_buttons = poll.build_vote_buttons() if mode == 'vote' else poll.build_admin_buttons()
    send_message(0.5, chat_id=uid, text=poll_text, reply_markup=poll_buttons)

def update_user(uid, **kwargs):
    key = ndb.Key('User', uid)
    user = key.get()
    if not user:
        user = User(id=uid)
    user.populate(**kwargs)
    user.put()

def send_message(countdown=0, **kwargs):
    payload = json.dumps(kwargs)
    taskqueue.add(queue_name='outbox', url='/sendMessage', payload=payload, countdown=countdown)
    logging.info('Message queued')
    logging.debug(payload)

def edit_message_text(**kwargs):
    payload = json.dumps(kwargs)
    taskqueue.add(queue_name='outbox', url='/editMessageText', payload=payload)
    logging.info('Message text edit queued')
    logging.debug(payload)

def edit_message_reply_markup(**kwargs):
    payload = json.dumps(kwargs)
    taskqueue.add(queue_name='outbox', url='/editMessageReplyMarkup', payload=payload)
    logging.info('Message reply markup edit queued')
    logging.debug(payload)

app = webapp2.WSGIApplication([
    ('/', FrontPage),
    ('/' + BOT_TOKEN, MainPage),
    ('/sendMessage', SendMessagePage),
    ('/editMessageText', EditMessageTextPage),
    ('/editMessageReplyMarkup', EditMessageReplyMarkupPage),
], debug=True)
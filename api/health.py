from cloud import Handler

class handler(Handler):
    def do_GET(self):
        self.reply(200)

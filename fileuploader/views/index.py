from base import BaseView

class IndexView(BaseView):

    url = r"^$"
    
    def get(self, *args, **kargs):

        return self.render_to_response({})
        
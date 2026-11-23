"""Tk-thread event delivery with cancellable subscriptions."""
from collections import defaultdict
from queue import SimpleQueue,Empty
import threading

TOPICS=('selection.changed','element.open','network.edited','results.updated','results.stale','theme.changed','language.changed','study.event.requested','diagram.overlay.changed')
class EventBus:
    def __init__(self,root=None):
        self._callbacks=defaultdict(dict)
        self._tokens={}
        self._counter=0
        self._thread=threading.get_ident()
        self._queue=SimpleQueue()
        self.root=None
        self._timer=None
        if root is not None:self.attach(root)
    def attach(self,root):
        self.close()
        self.root=root
        self._thread=threading.get_ident()
        self._timer=root.after(25,self._poll)
        root.bind('<Destroy>',lambda e:self.close() if e.widget is root else None,add='+')
    def subscribe(self,topic,callback):
        self._counter+=1
        token=self._counter
        self._callbacks[topic][token]=callback
        self._tokens[token]=topic
        return token
    def unsubscribe(self,token):
        topic=self._tokens.pop(token,None)
        if topic is not None:self._callbacks[topic].pop(token,None)
    def publish(self,topic,**payload):
        if threading.get_ident()!=self._thread:
            if self.root is None:raise RuntimeError('Attach a Tk root before publishing from a worker')
            self._queue.put((topic,payload))
            return
        for callback in tuple(self._callbacks[topic].values()):callback(**payload)
    def _poll(self):
        self._timer=None
        try:
            while True:
                topic,payload=self._queue.get_nowait()
                self.publish(topic,**payload)
        except Empty:pass
        if self.root is not None:self._timer=self.root.after(25,self._poll)
    def close(self):
        if self.root is not None and self._timer:
            try:self.root.after_cancel(self._timer)
            except Exception:pass
        self._timer=None
        self.root=None

bus=EventBus()

"""Shared diagram/property controller bound to the document network session."""
from copy import deepcopy
from .controller import EditorController
from ..i18n import tr


class DiagramEditContext:
    def __init__(self,app,session):
        self.app,self.network_session,self.root=app,session,app.root_path

    def get_snapshot(self):
        return dict(model=self.network_session.model,study=self.app.document.snapshot())

    def select_element(self,identity):
        if identity in self.app.index:self.app.select(identity)

    def log(self,level,message):self.app.output.log(level,message)

    def publish_result(self,result):
        self.app.publish_parity_result(result,'network_editor')
        lf=result.get('payload',{}).get('loadflow')
        if lf:self.app.loadflow_done(lf)

    def submit_job(self,module,request,on_message):
        from power_machine.netedit import execute_request
        return self.app.start_task(tr('edit.flow'),lambda:execute_request(deepcopy(request)),
                                   lambda result:on_message(('result',result)))


def get_editor_controller(app):
    """Return one shared EditorController for app.document.network.

    G-TOOLS may reuse this controller for ElementEditor. It publishes committed
    edits and stale results on app.bus and refreshes the application's model.
    Replacing the document replaces the controller, never the shared history.
    """
    session=app.document.network
    old=getattr(app,'_diagram_edit_controller',None)
    if old is not None and old.session is session:return old
    if old is not None:
        old.session.unsubscribe(app._diagram_edit_subscription)
        old.close()
    if session.state is None:
        from power_machine.netedit import new_variant
        from power_machine.netedit.schema import variant_to_dict
        session.load_data(app.base_model,dict(schema='power_machine-gui-network-v1',
            variant=variant_to_dict(new_variant(app.base_model)),drafts={}))
    controller=EditorController(DiagramEditContext(app,session),session)
    app._diagram_edit_controller=controller
    from power_machine.netedit.commands import index_model
    previous=index_model(session.model)
    def changed(event):
        current=index_model(session.model)
        changed_ids=sorted(key for key in set(previous)|set(current) if previous.get(key)!=current.get(key))
        previous.clear()
        previous.update(current)
        for view in list(app.diagrams.values()):
            view.positions.update({key:(s.x,s.y) for key,s in view.scene.symbols.items()})
        if event['electrical_changed']:
            app.network_changed()
            app.bus.publish('network.edited',ids=changed_ids)
            app.bus.publish('results.stale',reason=tr('edit.network_changed'))
        else:
            for view in list(app.diagrams.values()):
                view._rebuild_scene()
                view.draw()
    app._diagram_edit_subscription=session.subscribe(changed)
    if not getattr(app,'_diagram_editor_cleanup_bound',False):
        def cleanup(event):
            if event.widget is app:
                current=getattr(app,'_diagram_edit_controller',None)
                if current:
                    current.session.unsubscribe(app._diagram_edit_subscription)
                    current.close()
        app.bind('<Destroy>',cleanup,add='+')
        app._diagram_editor_cleanup_bound=True
    return controller

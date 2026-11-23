"""Portable, tolerant user preferences. No directories are created on import."""
import json,os,sys
from pathlib import Path

DEFAULTS=dict(theme='light',language=None,main_geometry='1400x850',diagram_geometry='1200x760',docked=False,recent_files=[],panel_options={})
def config_directory():
    override=os.environ.get('POWER_MACHINE_CONFIG_DIR')
    if override:return Path(override).expanduser()
    if sys.platform=='win32':return Path(os.environ.get('APPDATA',Path.home()/'AppData/Roaming'))/'PowerMachine'
    if sys.platform=='darwin':return Path.home()/'Library/Application Support/PowerMachine'
    return Path(os.environ.get('XDG_CONFIG_HOME',Path.home()/'.config'))/'power_machine'
class Settings:
    def __init__(self,path=None):
        self.path=Path(path) if path else config_directory()/'settings.json'
        self.load()
    def load(self):
        from copy import deepcopy
        self.data=deepcopy(DEFAULTS)
        try:
            value=json.loads(self.path.read_text(encoding='utf8'))
            if isinstance(value,dict):
                for key, item in value.items():
                    default = DEFAULTS.get(key)
                    if key in DEFAULTS and default is not None and not isinstance(item,type(default)):
                        continue
                    self.data[key] = item
                if self.data['language'] not in ('en','es',None): self.data['language'] = None
                if self.data['theme'] not in ('light','dark'): self.data['theme'] = 'light' 
        except (OSError,ValueError,TypeError):pass
        return self
    def get(self,key,default=None):return self.data.get(key,default)
    def set(self,key,value):self.data[key]=value
    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf8')
        temporary.replace(self.path)
settings=Settings()

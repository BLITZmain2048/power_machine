"""Adaptación de presentación de PlotPage para la ventana de ingeniería."""
from .plots import PlotPage, display_series


class EngineeringPlotPage(PlotPage):
    def _series(self, samples, key):
        """Conserva la unidad de cada señal y explicita la base de frecuencia."""
        if getattr(self,'spec',{}).get('axis_specs'):
            return PlotPage._series(self,samples,key)
        unit=key.rsplit('|',1)[-1]
        x,y=samples.series(key)
        if unit == 'Hz':
            mode,base_hz=self._frequency_options()
            return display_series('presentation|m:fehz|Hz',x,y,mode,base_hz)
        return x,y,unit

    def redraw(self, force=False):
        super().redraw(force=force)
        for axis in self.axes:
            if axis.get_yscale()=='linear':axis.ticklabel_format(axis='y',style='plain',useOffset=False)

"""Alcance visible de la evidencia, sin otorgar validación por cargar una curva."""
from .i18n import tr, translate_text


def comparison_context(page=None, result=None, module=None):
    label = (result or {}).get('gui_validation_label', '')
    if getattr(page, 'reference', None) is not None:
        reference = getattr(page, 'reference_label', 'REF')
        badge = tr('int2.ref_reference_only') if reference == 'REF' or reference.endswith(' / REF') else tr('int2.ref_no_comparison')
        return badge, tr('int2.ref_reference_detail', reference=reference)
    if label and label.startswith(('cotejado con la referencia externa', 'compared with the external reference')):
        from .display import public_text
        # La etiqueta solo la publica el cotejador tras verificar caso, perfil y magnitudes.
        return tr('int2.ref_sc'), tr('int2.ref_sc_detail', label=public_text(translate_text(label)))
    return tr('int2.ref_no_comparison'), tr('int2.ref_no_comparison_detail', module=module or tr('int2.current_view'))

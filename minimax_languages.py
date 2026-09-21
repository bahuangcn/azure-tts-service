"""MiniMax-only language metadata, never a list of additional available voices.

References checked 2026-09-21:
https://platform.minimax.io/docs/faq/system-voice-id
https://github.com/MiniMax-AI/skills/blob/main/skills/frontend-dev/references/minimax-voice-catalog.md

The API often omits language and describes only an accent. Use explicit fields,
then official system-voice categories, then descriptions. Do not classify custom
voices from arbitrary IDs, or confuse model-wide multilingual support with a
voice's catalog language. Keep regional evidence and metadata provenance.
"""
import re

LANGUAGES = {
    'zh-CN': ('Chinese (Mandarin)', 'Mandarin', '普通话', '中文', '汉语'),
    'zh-HK': ('Cantonese', '粤语'),
    'en': ('English', '英语', '英文'), 'ja': ('Japanese', '日语'),
    'ko': ('Korean', '韩语'), 'fr': ('French', '法语'), 'de': ('German', '德语'),
    'es': ('Spanish', '西班牙语'), 'pt': ('Portuguese', '葡萄牙语'),
    'ru': ('Russian', '俄语'), 'ar': ('Arabic', '阿拉伯语'), 'it': ('Italian', '意大利语'),
    'hi': ('Hindi', '印地语'), 'tr': ('Turkish', '土耳其语'),
    'vi': ('Vietnamese', '越南语'), 'th': ('Thai', '泰语'), 'id': ('Indonesian', '印尼语'),
    'nl': ('Dutch', '荷兰语'), 'uk': ('Ukrainian', '乌克兰语'),
    'pl': ('Polish', '波兰语'), 'ro': ('Romanian', '罗马尼亚语'),
    'el': ('Greek', '希腊语'), 'cs': ('Czech', '捷克语'), 'fi': ('Finnish', '芬兰语'),
}
LEGACY_ZH = set('male-qn-qingse male-qn-jingying male-qn-badao male-qn-daxuesheng female-shaonv female-yujie female-chengshu female-tianmei clever_boy cute_boy lovely_girl cartoon_pig bingjiao_didi junlang_nanyou chunzhen_xuedi lengdan_xiongzhang badao_shaoye tianxin_xiaoling qiaopi_mengmei wumei_yujie diadia_xuemei danya_xuejie Arrogant_Miss Robot_Armor'.split())
LEGACY_ZH.update(v + '-jingpin' for v in list(LEGACY_ZH) if v.startswith(('male-qn-', 'female-')))
LEGACY_EN = set('Santa_Claus Grinch Rudolph Arnold Charming_Santa Charming_Lady Sweet_Girl Cute_Elf Attractive_Girl Serene_Woman'.split())
ENGLISH_ACCENTS = {
    'en-US': ('美式口音', '美国口音', '美式英语', 'american accent', 'american english'),
    'en-GB': ('英式口音', '英国口音', '英式英语', 'british accent', 'british english'),
    'en-AU': ('澳大利亚口音', '澳洲口音', '澳大利亚英语', 'australian accent', 'australian english'),
    'en-IN': ('印度口音', '印度英语', 'indian accent', 'indian english'),
    'en-CA': ('加拿大口音', '加拿大英语', 'canadian accent', 'canadian english'),
}


def canonical_language(value):
    value = str(value).strip().replace('_', '-')
    for code, names in LANGUAGES.items():
        if value.casefold() in (n.casefold() for n in names):
            return code
    if re.fullmatch(r'[a-zA-Z]{2,3}(?:-[a-zA-Z]{2}|-[a-zA-Z]{4})?(?:-[a-zA-Z]{2})?', value):
        parts = value.split('-')
        return '-'.join([parts[0].lower()] + [p.title() if len(p) == 4 else p.upper() for p in parts[1:]])
    return None


def resolve_languages(raw, source, structured):
    explicit = list(dict.fromkeys(code for v in structured if (code := canonical_language(v))))
    if explicit:
        return explicit, 'official_field'
    voice_id = raw['voice_id'].strip()
    description = raw.get('description') or []
    text = (' '.join(map(str, description)) if isinstance(description, list) else str(description)).casefold()
    codes, provenance = [], 'unmarked'
    if source == 'system':
        if voice_id in LEGACY_ZH:
            codes = ['zh-CN']
        elif voice_id in LEGACY_EN:
            codes = ['en']
        else:
            for code, names in LANGUAGES.items():
                if voice_id.casefold().startswith(names[0].casefold() + '_'):
                    codes = [code]
                    break
        if codes:
            provenance = 'official_catalog'
    if not codes:
        codes = [code for code, names in LANGUAGES.items() if any(
            re.search(r'\b' + re.escape(name.casefold()) + r'\b', text) if name.isascii()
            else name in text for name in names)]
        if 'zh-HK' in codes and 'zh-CN' in codes and not any(n in text for n in ('普通话', 'mandarin')):
            codes.remove('zh-CN')
        if codes:
            provenance = 'official_description'
    # Only refine an established English category; an Indian accent alone does
    # not imply that an unknown custom voice is English rather than Hindi.
    if 'en' in codes:
        regions = [code for code, names in ENGLISH_ACCENTS.items() if any(n in text for n in names)]
        if regions:
            codes = [c for c in codes if c != 'en'] + regions
            provenance += '+accent_description'
    return list(dict.fromkeys(codes)), provenance

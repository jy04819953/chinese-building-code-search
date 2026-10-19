"""全库共用常量：正则、阈值、默认路径（按源文件顺序，常量间可互相引用）。"""
import os, re

STANDARDS_DIR = os.environ.get('STDQ_DATA_DIR') or os.path.expanduser('~/.stdq')

DB_PATH = os.path.join(STANDARDS_DIR, 'standards.db')

THEME_KEYWORDS = {
    'structure': ['混凝土', '钢筋', '梁', '柱', '剪力墙', '砌体', '钢结构',
                  '焊缝', '挠度', '裂缝', '承载力', '荷载', '基础', '桩基',
                  '地基', '筏板', '抗震', '钢筋保护层'],
    'plumbing':  ['地漏', '排水', '给水', '通气', '立管', '存水弯', '化粪池',
                  '水封', '污水', '废水', '雨水管', '水泵', '同层排水'],
    'hvac':      ['空调', '通风', '采暖', '防排烟', '风口', '风管', '冷凝水',
                  '锅炉', '热泵', '散热器', '新风', '排烟', '厨房油烟'],
    'electrical':['配电', '强电', '弱电', '插座', '照明', '接地', '漏电',
                  'SPD', '防雷', '等电位', '母线', '配电箱', '开关',
                  '漏电保护', '应急照明'],
    'barrier_free': ['无障碍', '轮椅', '坡道', '扶手', '盲道', '缘石坡道',
                     '低位', '无障碍厕所'],
    'energy':    ['保温', '隔热', '传热系数', 'K值', 'K 值', '气密性',
                  '节能', 'SHGC', '遮阳', '窗墙比'],
    'gas':       ['燃气', '煤气', '天然气', '燃气表', '燃气立管', '燃气管道',
                  '燃气热水器', '燃气报警'],
    'curtain_wall': ['幕墙', '玻璃幕墙', '石材幕墙', '金属幕墙', '人造板幕墙',
                         '压顶', '型材', '立柱', '横梁', '挂件', '硅酮胶'],
        # 新增 4 个主题(13 主题配套)
        'fire':          ['防火', '消防', '火灾', '烟', '火警', '耐火等级',
                         '疏散', '防火分区', '防火间距', '防火门', '灭火器',
                         '消火栓', '防烟', '排烟', '防火卷帘'],
        'civil_defense': ['人防', '人民防空', '防空', '防护', '战时',
                          '平战结合', '防空地下室'],
        'waterproof':    ['防水', '防水层', '卷材', '涂膜', '刚性防水',
                          '柔性防水', '防水卷材', '防水涂料', '抗渗',
                          '防水材料', '防水构造'],
        'roof':          ['屋面', '屋面工程', '坡屋面', '平屋面', '种植屋面',
                         '倒置式屋面', '屋面防水', '屋面排水', '屋面保温',
                         '屋面构造'],
    }

THEME_BONUS = 0.08

THEME_NAMES = {
    'structure':     '结构',
    'plumbing':      '给排水',
    'hvac':          '暖通',
    'electrical':    '电气',
    'barrier_free':  '无障碍',
    'energy':        '节能',
    'gas':           '燃气',
    'curtain_wall':  '幕墙门窗',
    'fire':          '防火',
    'civil_defense': '人防',
    'waterproof':    '防水',
    'roof':          '屋面',
    'general':       '通用(兜底)',
}

SEMANTIC_EXPAND_THRESHOLD = 0.7

CATALOG_PATH = os.path.join(STANDARDS_DIR, 'catalog.md')

_TYPE_MAP_PATH = os.environ.get('STDQ_TYPE_MAP') or os.path.join(STANDARDS_DIR, 'type_map.json')

_TYPE_MAP = None  # 延迟初始化（见 catalog.py 末尾）

_embed_model = None

_embed_embeddings = None

_embed_ids = None

# daemon socket 默认跟着数据目录走（避免多个库/多实例串用同一个 /tmp socket）
SOCK_PATH = os.environ.get('STDQ_SOCK') or os.path.join(STANDARDS_DIR, 'stdq_daemon.sock')

_daemon_no = False


_TOC_SKIP_RE = re.compile(
    r"本(?:规范|标准|规程)用词说明|引用标准名录|附[::]?条文说明|条文说明(?!\S)"
)

_TOC_TAIL_RE = re.compile(
    r"(?<!\d)[\.…]{1,}(?:\s+[\.…]{1,})*(\s*[（(]?\d+[）)]?)?\s*$"
)

_RE_OCR_FUSED_NUM = re.compile(r'(\d)\s*\.\s*(\d)')

_TERM_NUM_RE = re.compile(r'(\d+\.\d+(?:\.\d+){0,3})')

_TERM_LINE_RE = re.compile(
    r'^(?:##\s*)?(\d+\.\d+(?:\.\d+){0,3})\s*'
    r'([\u4e00-\u9fff\[\]（）()\-,、·]*(?:\([^)]*\)[\u4e00-\u9fff\[\]（）()\-,、·]*)*)\s+'
    r'([a-zA-Z][a-zA-Z;,\[\]\-\(\)0-9/ ]*?)(?=\s*[\u4e00-\u9fff]|$)'
)

# 兜底变体（原版匹配不上时才试）：覆盖三种实测形态 ——
#   ① 术语名含 `&`：`## 3.5.24会展中心meeting & exhibition center`
#   ② 术语名与英文之间夹公式：`## 3.11.34采暖耗煤量指标 $( Q _ { \mathrm{c} } )$ index of ...`
#   ③ 英文里带 ℃ / $LaTeX$：`## 3.11.37采暖度日数(HDD18) heating degree day based on 18℃`
# ⚠️ 不能直接放宽上面那条原正则：会让"定义行"被误判成"新术语行"，已有术语释义被截短。
_TERM_LINE_RE_V2 = re.compile(
    r'^(?:##\s*)?(\d+\.\d+(?:\.\d+){0,3})\s*'
    r'([\u4e00-\u9fff\[\]（）()\-,、·&]*(?:\([^)]*\)[\u4e00-\u9fff\[\]（）()\-,、·&]*)*)\s*'
    r'(?:\$[^$\n]*\$\s*)?'
    r'([a-zA-Z][a-zA-Z;,\[\]\-\(\)0-9/ &$\\{}^{}_~.°℃]*?)(?=\s*[\u4e00-\u9fff]|$)'
)

# 术语本「总则」条文：`1.0.1为统一和规范…`（N.0.M + 中文正文，无英文术语）。
# 术语提取器只管「编号+术语+英文」，所以 1 总则那几条历来收不到。
_TERM_GENERAL_RE = re.compile(r'^(?:##\s*)?(\d+\.0\.\d+)\s*([\u4e00-\u9fff][^\n]{5,})$')

_GLUED_TERM_RE = re.compile(
    r'^(?:##\s*)?(\d+\.\d+(?:\.\d+){0,3})\s*'
    r'([\u4e00-\u9fff\[\]（）()\-,、·]*?(?:\([^)]*\)[\u4e00-\u9fff\[\]（）()\-,、·]*?)*?)'
    r'([a-zA-Z][a-zA-Z;,\[\]\-\(\)0-9/ ]*?)(?=\s*[\u4e00-\u9fff]|$)'
)

_TERM_NUM_ONLY_RE = re.compile(r'^(?:##\s*)(\d+\.\d+(?:\.\d+){0,3})\s*$')

_TERM_HEAD_RE = re.compile(r'^(?:##\s*)?([\u4e00-\u9fff\[\]（）()\-,、·]+)\s+([a-zA-Z].*)$')

RE_APX_HEAD = re.compile(r'^#{0,4}\s*附录\s*([A-Z])\s*(.*)$')

RE_APX_SEC = re.compile(r'^#{0,4}\s*([A-Z])\s*[.．]\s*(\d{1,2})\s*$'
                        r'|^#{0,4}\s*([A-Z])\s*[.．]\s*(\d{1,2})\s+([^0-9\-–].*)$')

RE_APX_CL = re.compile(r'^#{0,4}\s*([A-Z])\s*[.．]\s*(\d{1,2})\s*[.．]\s*(\d{1,2})'
                       r'(?!\s*[.．]\s*\d)(?![0-9\-–])'   # 排除 4 段编号(A.1.2.3)与表号(A.0.1-1)
                       r'[ \t]*([^\s].*)?$')

_APX_NUM = r'[A-Z]\s*[.．]\s*\d{1,2}\s*[.．]\s*\d{1,2}'

_APX_SEP = r'(?:[、,，]|和|及|～|~|—|–|-|至|\\~)'

RE_APX_CL_RUN = re.compile(r'^#{0,4}\s*(' + _APX_NUM + r'(?:\s*' + _APX_SEP + r'\s*' + _APX_NUM + r')*)'
                           r'(?!\s*[.．]\s*\d)(?![0-9\-–])[ \t]*([^\s].*)?$')

RE_APX_STOP = re.compile(r'^#{0,4}\s*(条文说明|起草说明|编制说明|制订说明|附[：:]?\s*(条文说明|起草说明))')

RE_APX_STOP_FUZZY = re.compile(r'^#*\s*[A-Za-z0-9\-\u4e00-\u9fff]{0,14}条文说明[A-Za-z0-9\-\u4e00-\u9fff]{0,14}\s*[:：]?\s*$')

RE_TOC_PAGENUM = re.compile(r'[（(]?\s*\d{1,4}\s*[)）]?\s*$')

RE_APX_GLUED = re.compile(r'[。；](?=\s*[A-Z]\s*[.．]\s*\d{1,2}\s*[.．]\s*\d{1,2}(?![0-9])[ \t]+[^\s])')

RE_APX_TBL = re.compile(r'^#{0,4}\s*表\s*[A-Z]')

RE_APX_FIG = re.compile(r'^#{0,4}\s*图\s*[A-Z]')

APX_DUP_DROPPED = []

NOTES_KEYWORDS = ['条文说明', '起草说明', '编制说明', '制订说明']

RE_DOC_TAIL_HEAD = re.compile(
    r'^\s*#{1,6}\s*(?:附[：:]?\s*)?[\u4e00-\u9fff]{0,8}?(?:用词说明|引用标准名录|参考文献)')

_STD_CODE_RE = r'([A-Z]{2,}[-—]?\s*\d+(?:[-—]\s*\d+)*(?:\s*\d{4})?)'

"""Guard the model's citation exemption without deciding product eligibility."""

import re
import unicodedata

PRELIMINARY_NOTICE = "概括性条款按初筛口径直接通过，未逐项审核，不代表已取得银行审批或合规认证。"

# Guards on an evidence exemption, not a local product condition tree.
_CONCRETE = re.compile(
    r"资质|资格|证书|认证|认定|许可证?|执照|名录|名单|目录|编号|标签|入库|备案|登记|"
    r"国家高新技术企业|科技型中小企业|专精特新|高层次人才|"
    r"批准|审批|认可|核准|审核通过|审查通过|开户|开立|账户|基本户|授信额度|保证金|"
    r"质押|专利|著作权|知识产权|商标|投资基金|实际到位|未退出|实缴|权属|投保|保单|承保|"
    r"签订.{0,8}合同|签署.{0,8}协议|"
    r"不良.{0,4}记录|失信|司法冻结|违法.{0,4}记录|处罚|责任事故|恶意逃废债|征信报告|信用报告|查询结果|"
    r"长于|短于|不低于|不高于|不少于|不超过|大于|小于|至少|至多|"
    r"[零〇一二两三四五六七八九十百千万亿半]+[个项次年月日天岁元万亿%％]|百分之|千分之|"
    r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|\d"
)

# These phrases name no specific licence, report, period or risk-list check.
# Remove only the generic wording when checking the evidence exemption;
# concrete requirements elsewhere in the same clause remain guarded.
_GENERAL_REQUIREMENTS = re.compile(
    r"(?:具备|具有)(?:相关)?业务经验[与及和](?:相关|相应)?资质|"
    r"(?:具备|具有)(?:参与基础交易的经营|从事相关业务的|相应业务|相关业务|业务|经营)(?:资格|资质)|"
    r"无(?:重大|严重)?(?:不良(?:信用|征信)?记录|环保违法记录|违法违规记录|违法记录|"
    r"责任事故|违法违规事件|恶意逃废债|欺诈|司法冻结|失信行为|失信记录)|"
    r"(?:不存在|未发生)(?:司法冻结|失信行为|不良征信记录)"
)
_NUMBERED_START = re.compile(r"(?:\d+[.、．](?!\d)|[（(][一二三四五六七八九十\d]+[）)])")


def preliminary_scope(quote: str, source: str) -> bool:
    """Check a model-identified broad clause; never promote UNKNOWN/FAIL to PASS."""
    quote = quote.replace("\r\n", "\n").replace("\r", "\n")
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if not quote.strip():
        return False
    offset = source.find(quote)
    if offset < 0:
        return False
    # Avoid a permissive fragment embedded inside a more specific requirement.
    left, right = source[:offset].rstrip(" \t"), source[offset + len(quote) :].lstrip(" \t")
    # Imported source sometimes glues the next item directly to the preceding
    # sentence ("账户3.企业具有..."). A quoted item number is a real boundary.
    numbered = _NUMBERED_START.match(quote.lstrip())
    if (
        left
        and left[-1] not in "，,；;。:：\n、）).．"
        and not left.endswith(("且", "并", "和", "及"))
        and not numbered
    ):
        return False
    if (
        right
        and right[0] not in "，,；;。:：\n、"
        and not right.startswith(("且", "并", "和", "及"))
        and not re.match(r"\d+[.、．]", right)
    ):
        return False
    if re.search(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]", quote):
        return False
    normalized = unicodedata.normalize("NFKC", quote)
    # Clause numbering isn't a business threshold; other digits remain guarded.
    normalized = re.sub(r"(^|[;；。:：\n])\s*(?:[（(]?[一二三四五六七八九十\d]+[）).、．])\s*", r"\1", normalized)
    normalized = re.sub(r"(?<=[\u4e00-\u9fff])\d+[.、．](?=[\u4e00-\u9fff])", "", normalized)
    normalized = _GENERAL_REQUIREMENTS.sub("", normalized)
    return _CONCRETE.search(normalized) is None

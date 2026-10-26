"""Generate the initial DRAFT snapshot only; refuses to overwrite an existing file.

Run from the repository root with ``uv run python -m data.build_initial_rules``.
This bootstrap is not a replacement for reviewing changes to published rules.
"""
import csv
import json
from pathlib import Path

from app.catalog import CUSTOMER_COLUMN, canonical_product_url, source_record_hash

ROOT = Path(__file__).resolve().parents[1]


def condition(i, quote, *, field=None, operator="manual", expected=None,
              source="申请条件", label=None, **kwargs):
    node = {"id": i, "kind": "condition", "operator": operator,
            "source_field": source, "source_quote": quote, "label": label or quote,
            "required_material": "请提供能够证明该条款的有效材料，并核对银行适用口径。"}
    if field:
        node["field_id"] = field
    if operator != "manual":
        node["expected"] = expected
    node.update(kwargs)
    return node


def group(i, children, kind="all", quote="", source="申请条件"):
    return {"id": i, "kind": kind, "children": children,
            "source_quote": quote, "source_field": source}


def qualification(i, field, quote, source="申请条件"):
    return condition(i, quote, field=field, operator="eq", expected=True,
                     source=source, require_valid_until=True,
                     required_material="提供申请企业的有效资质认定文件、认定主体及有效期。")


def build():
    target = ROOT / "data" / "product_rules.json"
    if target.exists():
        raise SystemExit("拒绝覆盖既有规则文件；请通过人工复核流程更新规则及版本。")
    with (ROOT / "科技金融产品.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    products = {}
    for row in rows:
        pid, _ = canonical_product_url(row["详情URL"])
        if pid in products:
            continue
        name = row["产品名称"]
        app = row["申请条件"]
        customer = row[CUSTOMER_COLUMN]

        def leaf(suffix, quote, **kwargs):
            return condition(pid + "." + suffix, quote, **kwargs)

        def node(suffix, children, **kwargs):
            return group(pid + "." + suffix, children, **kwargs)

        tree = node("all", [
            leaf("application", app, label="完整申请条件待逐项核验"),
            leaf("customers", customer, source=CUSTOMER_COLUMN, label="适用客户必要限制待核验"),
        ])
        if name == "科企贷":
            tree = node("all", [
                leaf("size", customer, field="company.size_category", operator="in",
                     expected=["small", "micro"], source=CUSTOMER_COLUMN, label="属于小型或微型企业"),
                leaf("technology", customer, field="business.is_technology", operator="eq",
                     expected=True, source=CUSTOMER_COLUMN, label="属于科创型企业"),
                node("ip_or_talent", [
                    leaf("ip_i", "拥有 1项以上 I类自主知识产权", field="ip.class_i_count",
                         operator="gte", expected=1, label="I类自主知识产权至少1项"),
                    leaf("ip_ii", "2项以上 I I类自主知识产权", field="ip.class_ii_count",
                         operator="gte", expected=2, label="II类自主知识产权至少2项"),
                    leaf("talent", "企业主/主要股东获得国家或地方政府颁发的高层次人才称号",
                         subject_id="enterprise_owner_or_major_shareholder",
                         required_material="提供人才称号证明及该人员与申请企业的股权或企业主关系。"),
                ], kind="any", quote=app),
            ])
        elif name == "智企贷":
            tree = node("all", [
                leaf("size", customer, field="company.size_category", operator="in",
                     expected=["small", "micro"], source=CUSTOMER_COLUMN, label="属于小微企业"),
                leaf("technology", app, field="business.is_technology", operator="eq",
                     expected=True, label="属于科创型企业"),
                leaf("ai", customer, field="business.is_ai", operator="eq", expected=True,
                     source=CUSTOMER_COLUMN, label="属于人工智能企业"),
                leaf("bank_assessment", app, label="北部湾银行认可细分领域及商业前景",
                     reason_code="BANK_CONFIRMATION_REQUIRED"),
            ])
        elif name == "农银广西创新积分贷":
            quals = [
                qualification(pid + ".high_tech", "qualifications.high_tech", "国家高新技术企业", CUSTOMER_COLUMN),
                qualification(pid + ".technology_sme", "qualifications.technology_sme", "科技型中小企业", CUSTOMER_COLUMN),
                qualification(pid + ".specialized", "qualifications.specialized", "“专精特新”中小企业", CUSTOMER_COLUMN),
                leaf("other_qualifications", "4.专精特新“小巨人”企业；5.国家技术创新示范企业；6.国家级制造业单项冠军企业；7.省级创新型中小企业。",
                     source=CUSTOMER_COLUMN, label="其余四类认可资质中至少一项"),
            ]
            clauses = app.split("；")
            tree = node("all", [
                leaf("enrollment", "纳入广西创新积分制管理", source=CUSTOMER_COLUMN),
                leaf("national_score", "全国创新积分、广西创新积分均不低于60分", source=CUSTOMER_COLUMN,
                     field="innovation.national_score", operator="gte", expected=60,
                     label="全国创新积分不低于60分"),
                leaf("guangxi_score", "全国创新积分、广西创新积分均不低于60分", source=CUSTOMER_COLUMN,
                     field="innovation.guangxi_score", operator="gte", expected=60,
                     label="广西创新积分不低于60分"),
                node("qualifications", quals, kind="any", quote=customer, source=CUSTOMER_COLUMN),
                node("growth_or_new_registration", [
                    leaf("revenue_growth", "近3年中至少1年营业收入实现增长",
                         required_material="提供覆盖比较期的年度营业收入及同主体同口径增长计算。"),
                    leaf("new_registration", "新注册企业除外", label="新注册企业豁免需银行确认",
                         reason_code="BANK_CONFIRMATION_REQUIRED"),
                ], kind="any", quote=clauses[0]),
                *[leaf("additional_" + str(i), quote,
                       subject_id="company_and_related_persons" if i in (1, 2) else "company")
                  for i, quote in enumerate(clauses[1:], start=1)],
            ])
        elif name == "科创贷" and row["机构名称"] == "招商银行":
            tree = node("all", [
                node("qualifications", [
                    qualification(pid + ".specialized", "qualifications.specialized", "专精特新企业"),
                    qualification(pid + ".high_tech", "qualifications.high_tech", "国家高新技术企业"),
                    qualification(pid + ".technology_sme", "qualifications.technology_sme", "科技型中小企业"),
                    leaf("other_qualifications", "等国家部委牵头认定及重点培育的科技企业资质"),
                ], kind="any", quote="具有专精特新企业、国家高新技术企业、科技型中小企业等国家部委牵头认定及重点培育的科技企业资质"),
                leaf("established", "成立年限满1年", field="company.established_date",
                     operator="age_years_gte", expected=1, label="成立满1周年"),
                leaf("compliance", "生产经营合法合规"),
                leaf("credit_and_litigation", "借款人及法定代表人/实际控制人均无不良征信记录、无大额待执行诉讼、无司法冻结",
                     subject_id="company_and_related_persons",
                     required_material="分别提供企业及相关责任人的征信和司法材料，由银行确认主体范围与大额口径。"),
            ])
        elif name == "出口闪贷":
            tree = node("all", [
                leaf("size", customer, source=CUSTOMER_COLUMN, field="company.size_category",
                     operator="in", expected=["small", "micro", "medium"], label="属于中小微企业"),
                leaf("export", customer, source=CUSTOMER_COLUMN, field="business.has_export",
                     operator="eq", expected=True, label="具有出口业务"),
                leaf("export_years", "企业经营出口业务满2年", field="business.export_started_date",
                     operator="age_years_gte", expected=2, label="经营出口业务满2周年"),
                leaf("export_amount", "企业近1年海关出口量在100万美元以上",
                     field="business.export_usd_last_year", operator="gte", expected=1000000,
                     unit="USD", period="rolling_year", label="近1年海关出口金额不少于100万美元",
                     required_material="提供评估日前完整滚动一年的海关出口金额，币种美元且主体为申请企业。"),
                leaf("credit", "企业、法定代表人或实控人征信良好", subject_id="company_and_related_persons"),
                leaf("tax", "企业纳税信用良好", label="纳税信用良好（等级口径需银行确认）"),
                leaf("representative_age", "企业法定代表人年龄在22周岁（含）至65周岁（含）之间",
                     field="person.birth_date", operator="age_years_between", expected=[22, 65],
                     subject_id="legal_representative", label="法定代表人年龄为22至65周岁"),
                leaf("account", "企业在我行开立结算账户", label="已在招商银行开立结算账户"),
            ])
        elif name == "跨境直贷通":
            tree = node("all", [
                leaf("jurisdiction", "在我国境外（含我国香港、澳门和台湾地区）合法注册",
                     field="company.registration_jurisdiction", operator="in",
                     expected=["overseas", "hong_kong", "macao", "taiwan"],
                     label="申请主体注册地为境外或港澳台",
                     required_material="提供实际借款主体注册证书；境内母公司资料不能代替境外借款主体。"),
                leaf("registration", "合法注册", label="申请主体在当地合法注册"),
                leaf("non_financial", "非金融企业", field="business.is_financial", operator="eq", expected=False),
                leaf("customers", customer, source=CUSTOMER_COLUMN, label="具有符合产品范围的境外经营需求"),
            ])
        elif name == "科创e贷":
            tree = node("all", [
                node("qualification_or_score", [
                    node("recognized_qualification", [
                        qualification(pid + ".high_tech", "qualifications.high_tech", "科技型企业资质"),
                        qualification(pid + ".technology_sme", "qualifications.technology_sme", "科技型企业资质"),
                        qualification(pid + ".specialized", "qualifications.specialized", "科技型企业资质"),
                        leaf("other_qualification", "科技型企业资质", label="其他银行认可的科技型企业资质"),
                    ], kind="any"),
                    node("innovation_score", [
                        leaf("guangxi_score", "科技创新积分", field="innovation.guangxi_score",
                             operator="exists", expected=True, label="具有广西科技创新积分"),
                        leaf("national_score", "科技创新积分", field="innovation.national_score",
                             operator="exists", expected=True, label="具有全国科技创新积分"),
                        leaf("other_score", "科技创新积分", label="其他银行认可的科技创新积分"),
                    ], kind="any"),
                ], kind="any", quote=app),
                leaf("accepted_scope", app, label="银行确认资质或积分属于该产品认可口径",
                     reason_code="BANK_CONFIRMATION_REQUIRED"),
                leaf("customers", customer, source=CUSTOMER_COLUMN, field="business.is_technology",
                     operator="eq", expected=True, label="属于科技创新企业"),
            ])
        elif name == "专精特新贷" and "浙商" in row["机构名称"]:
            tree = node("all", [
                node("specialized_or_cultivation", [
                    qualification(pid + ".specialized", "qualifications.specialized", "各级“专精特新”及培育企业名单范围内的企业"),
                    leaf("cultivation", "培育企业名单范围内的企业", label="属于正式公布的培育企业名单"),
                ], kind="any"),
                leaf("government_list", app, label="地市级及以上政府经信部门正式公布且申请企业在名单内",
                     required_material="提供地市级及以上政府经信部门正式名单、申请企业匹配信息、发布日期和有效性。"),
            ])
        text = name + row["产品介绍"] + customer
        if name == "智企贷":
            tags = ["ai"]
        elif any(term in name for term in ["出口", "跨境", "境外", "备货贸易"]):
            tags = ["export"]
        elif any(term in text for term in ["科技", "科创", "专精特新", "高新技术"]):
            tags = ["technology"]
        else:
            tags = ["general_business"]
        products[pid] = {"product_name": name, "rule_version": "1.0.0-draft.1",
                         "source_record_sha256": source_record_hash(row), "rule_status": "DRAFT",
                         "availability_status": "SNAPSHOT", "rule_review": None,
                         "availability_verification": None, "relevance_tags": tags, "rules": tree}
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({"schema_version": "1.0",
                                 "description": "基于本地CSV的初始规则草稿；未经业务复核或实时银行可用性验证。",
                                 "products": products}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(products)} DRAFT product rule entries")


if __name__ == "__main__":
    build()

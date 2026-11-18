"""Measure official HTML route candidates on the isolated collector."""

from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx

from scraper.adapters._common import clean_tree, complete_body_paragraphs, extract_links, first_text, parse_date_zh
from scraper.fetcher import Fetcher


QIUSHI_FIRST_ISSUE = "https://www.qstheory.cn/20251231/2b7c1a0955524272be96ac77ccff34eb/c.html"
QIUSHI_SNAPSHOT = "https://web.archive.org/web/20260521082819id_/" + QIUSHI_FIRST_ISSUE
CASES = {
    "people": (
        ("commentary_index", "https://opinion.people.com.cn/GB/8213/49160/index.html", r"/n1/\d{4}/", ""),
        ("zhongsheng_index", "https://opinion.people.com.cn/GB/8213/49160/461972/index.html", r"/n1/\d{4}/", ""),
        ("edition_index", "https://paper.people.com.cn/rmrb/pc/layout/202611/17/node_09.html", r"/rmrb/pc/content/", ""),
        ("commentary_control", "https://paper.people.com.cn/rmrb/pc/content/202611/17/content_30191569.html", "", "人形机器人"),
        ("zhongsheng_control", "https://paper.people.com.cn/rmrb/pc/content/202609/16/content_30181273.html", "", "科技霸权"),
    ),
    "state_council": (
        ("policy_search", "https://sousuo.www.gov.cn/zcwjk/policyDocumentLibrary?q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD&t=zhengcelibrary",
         r"gov\.cn/.*(?:content_\d+\.htm|/content/)", ""),
        ("official_index", "https://www.gov.cn/zhengce/zuixin/", r"/zhengce/.*content_\d+\.htm", ""),
        ("official_control", "https://www.gov.cn/zhengce/content/202508/content_7037861.htm", "", "人工智能"),
        ("official_republication", "https://hbca.miit.gov.cn/xwdt/szyw/art/2025/art_e9f2b181839e47a8a6602f0e6e125398.html", "", "国发〔2025〕11号"),
    ),
    "cnki_zrbz": (
        ("issue", "https://zrbz.cbpt.cnki.net/portal/journal/portal/client/paper_list/type_benqi", r"/paper/", ""),
        ("article", "https://zrbz.cbpt.cnki.net/portal/journal/portal/client/paper/4cbf111f2127a9da9e671b463c22c57c", "", ""),
    ),
    "cnki_kxbz": (
        ("issue", "https://kxbz.cbpt.cnki.net/portal/journal/portal/client/paper_list/type_benqi", r"/paper/", ""),
        ("article", "https://kxbz.cbpt.cnki.net/portal/journal/portal/client/paper/0c12bb029efe6009bdb9ab76af071c4a", "", ""),
    ),
    "moe": (
        ("policy_index", "https://www.moe.gov.cn/jyb_xxgk/xxgk/neirong/fenlei/sxml_jyxxh/jyxxh_xxh/xxh_xgzc/",
         r"/srcsite/.*?/t\d+_\d+\.html$", ""),
        ("policy_control", "https://www.moe.gov.cn/srcsite/A16/s3342/202604/t20260410_1433240.html", "", "人工智能通识"),
        ("earlier_control", "https://www.moe.gov.cn/srcsite/A01/s7048/202504/t20250416_1187476.html", "", "资源开放目录"),
    ),
    "nda": (
        ("policy_index", "https://www.nda.gov.cn/sjj/zwgk/zcfb/list/index_pc_1.html",
         r"/sjj/zwgk/zcfb/\d{4}/[^/]+_pc\.html$", ""),
        ("data_control", "https://www.nda.gov.cn/sjj/zwgk/zcfb/0608/20260608172117399715004_pc.html", "", "强基扩容"),
        ("employment_control", "https://www.nda.gov.cn/sjj/zwgk/zcfb/0708/20260708133949899211227_pc.html", "", "人社部发"),
    ),
    "ndrc": (
        ("policy_index", "https://www.ndrc.gov.cn/xxgk/zcfb/tz/wap_index.html",
         r"/xxgk/zcfb/tz/\d{6}/t\d+_\d+\.html$", ""),
        ("policy_control", "https://www.ndrc.gov.cn/xxgk/zcfb/tz/202602/t20260210_1403680.html", "", "加强组织保障"),
    ),
    "approved_reprints": (
        ("moe_public_copy", "https://sjj.hunan.gov.cn/sjj/zwgk/zcfg/gjfg/202606/t20260608_33996235.html", "", "人工智能通识"),
        ("ndrc_public_copy", "https://www.jiangmen.gov.cn/bmpd/jmsfzhggj/ztlm/myjj/xwdt/content/post_3463758.html", "", "加强组织保障"),
    ),
    "miit_ethics": (
        ("hubei_copy", "https://jxt.hubei.gov.cn/bmdt/rdjj/202604/t20260407_5909143.shtml", "", "第三十七条"),
        ("agriculture_copy", "https://kjs.moa.gov.cn/gzdt/202605/t20260514_6484163.htm", "", "第三十七条"),
        ("commerce_index_record", "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=105505", "", "第三十七条"),
        ("ganzhou_copy", "https://www.ganxian.gov.cn/gxqxxgk/c111150/202604/1e4b4efb916841c5b93f210d841a176a.shtml", "", "第三十七条"),
        ("zhejiang_cac_copy", "https://www.zjwx.gov.cn/col/col1673566/art/2026/art_dc78f935ade4450c9f4e9aea8933fb33.html", "", "第三十七条"),
        ("ordos_copy", "https://www.ordos.gov.cn/zwgk/zc/xzgfxwj/xxyxgfxwj/zfjqbm/sgyhxxhj/202605/t20260515_2041041.html", "", "第三十七条"),
    ),
    "nea_energy": (
        ("issuer_policy", "https://www.nea.gov.cn/20260508/4dae97ca01d348e4871bb8654be34b3a/c.html", "", "国能发科技〔2026〕34号"),
        ("energy_bureau_copy", "https://xxgk.jcgov.gov.cn/szfgzbm/jcsnyj/fdzdgknr_31618/fgwj_31628/202605/t20260511_2348831.shtml",
         "", "国能发科技〔2026〕34号"),
    ),
}
BODY = ("div.TRS_Editor", ".trs_editor_view", "#UCAP-CONTENT", "#ozoom", "#articleContent",
        "#article-content", "#article", "#content", ".content", ".article-content", ".article_content",
        ".article", ".news-content", ".content-text", ".main-text", ".text_con", ".article_con",
        ".abstract", ".abstract-text", ".word", ".pages_content", ".b12c", "#con_con")
TITLE = ("h1", "h2", ".title", ".article-title", ".article_title", ".news-title", "#ti", ".share-title")
DATE = (".date", ".time", ".info", ".pubtime", ".source", ".article-info", ".article_info",
        ".article-date", ".article-source", ".pages-date", ".laiyuan", ".data-source",
        ".paper-info", ".content-info", ".publish-time", ".articleTime", "#pubtime", "time")


def inspect(fetcher, name, url, pattern, marker, domains):
    response = fetcher.fetch(url, domains)
    if not response.ok:
        return {"case": name, "status": response.status or 0,
                "robots_blocked": fetcher.stats["robots_blocked"],
                "fetch_errors": fetcher.stats["fetch_errors"]}
    tree = clean_tree(response.text)
    if pattern:
        links = extract_links(response.text, url, "a", domains, pattern)
        return {"case": name, "status": 200, "links": len(links),
                "inline_papers": len(tree.css("div.paperBox"))}
    bodies = []
    for selector in BODY:
        paragraphs = complete_body_paragraphs(tree, selector)
        if paragraphs:
            text = "\n".join(paragraphs)
            bodies.append([selector, len(paragraphs), len(text),
                           marker in text if marker else None])
    return {"case": name, "status": 200,
            "titles": [s for s in TITLE if first_text(tree, s)],
            "dates": [s for s in DATE if parse_date_zh(first_text(tree, s))],
            "bodies": bodies, "inline_papers": len(tree.css("div.paperBox"))}


def compare_qiushi_archive(fetcher):
    original_host = urlsplit(QIUSHI_FIRST_ISSUE).hostname
    archive_host = urlsplit(QIUSHI_SNAPSHOT).hostname
    original = fetcher.fetch(QIUSHI_FIRST_ISSUE, [original_host])
    archived = fetcher.fetch(QIUSHI_SNAPSHOT, [archive_host])
    result = {"case": "qiushi_snapshot", "publisher_status": original.status or 0,
              "archive_status": archived.status or 0, "robots_blocked": fetcher.stats["robots_blocked"]}
    if original.ok and archived.ok and archived.final_url == QIUSHI_SNAPSHOT:
        pattern = r"/\d{8}/[0-9a-f]{32}/c\.html?$"
        live_links, archive_links = (extract_links(response.text, QIUSHI_FIRST_ISSUE, "div.highlight a",
                                                   [original_host], pattern) for response in (original, archived))
        result.update(live_links=len(live_links), archive_links=len(archive_links),
                      same_links=bool(live_links) and live_links == archive_links)
    return result


def main():
    archive = sys.argv[1] == "wayback_qiushi"
    cases = CASES[sys.argv[1]] if not archive else ()
    domains = (sorted({urlsplit(url).hostname for _, url, _, _ in cases}) if not archive
               else [urlsplit(url).hostname for url in (QIUSHI_FIRST_ISSUE, QIUSHI_SNAPSHOT)])
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        fetcher = Fetcher(cache_dir=Path(os.environ.get("PUBLISHER_CACHE", "/tmp/publisher-probe")),
                          transport=httpx.HTTPTransport(trust_env=False),
                          strict_html_hosts={host: {} for host in domains})
        try:
            results = ([inspect(fetcher, *case, domains) for case in cases] if not archive
                       else [compare_qiushi_archive(fetcher)])
        finally:
            fetcher.close()
    for row in results:
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

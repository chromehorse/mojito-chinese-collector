"""Measure official HTML route candidates on the isolated collector."""

from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from scraper.adapters._common import clean_tree, complete_body_paragraphs, extract_links, first_text, parse_date_zh
from scraper.fetcher import Fetcher


QIUSHI_FIRST_ISSUE = "https://www.qstheory.cn/20251231/2b7c1a0955524272be96ac77ccff34eb/c.html"
QIUSHI_SNAPSHOT = "https://web.archive.org/web/20260521082819id_/" + QIUSHI_FIRST_ISSUE
QIUSHI_INTERVIEW = "https://www.qstheory.cn/20251231/18ec77b7dded4aa799e537c6068f8050/c.html"
QIUSHI_INTERVIEW_SNAPSHOT = "https://web.archive.org/web/20260417202126id_/" + QIUSHI_INTERVIEW
CAC_ROSTER_INDEX = "https://www.cac.gov.cn/2024-04/02/c_1713729983803145.htm"
CAC_ROSTER_LABELS = {f"生成式人工智能服务已备案和已登记信息（2026年{month}月）": month
                     for month in (2, 4, 6, 8)}
MEDIA_TYPE = re.compile(r"[a-z0-9.+-]{1,32}/[a-z0-9.+-]{1,64}")
CASES = {
    "wuhan_action_plan": (
        ("municipal_original",
         "https://www.wuhan.gov.cn/zwgk/xxgk/zfwj/bgtwj/202603/t20260323_2743481.shtml",
         "", "营造全社会共同参与的良好氛围"),
        ("municipal_mobile_edition",
         "https://3g.wuhan.gov.cn/zwgk/xxgk/zfwj/bgtwj/202603/t20260323_2743481.shtml",
         "", "营造全社会共同参与的良好氛围"),
    ),
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
    "early_ai_policy": (
        ("ndrc_original_accessible_edition", "https://www.ndrc.gov.cn/xxgk/zcfb/tz/202602/t20260210_1403680_ext.html",
         "", "加强组织保障"),
        ("ndrc_open_government_edition", "https://zfxxgk.ndrc.gov.cn/wap/iteminfo.jsp?id=20604", "", "加强组织保障"),
        ("fujian_procurement_copy", "https://fgw.fujian.gov.cn/ztzl/cjzxjyzx/zcfg/202602/t20260212_7098061.htm",
         "", "加强组织保障"),
        ("cac_anthropomorphic_services", "https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm", "", "本办法自2026年7月15日起施行"),
        ("gansu_ai_plan", "https://zwfw.gansu.gov.cn/guanghe/sbqd/gg/art/2026/art_69adfdba9218485b82cdd38c4336e0ff.html",
         "", "甘政发〔2026〕1号"),
        ("heilongjiang_ai_plan", "https://www.hlj.gov.cn/hlj/c108372/202602/c00_31915429.shtml", "", "此件公开发布"),
    ),
    "provincial_ai_policy": (
        ("gansu_full_official_copy", "https://zsj.pingliang.gov.cn/cyzc/art/2026/art_d5e1e5ab4183477bb85294c3153b18e4.html",
         "", "甘政发〔2026〕1号"),
        ("heilongjiang_secondary_official_copy", "https://nynct.hlj.gov.cn/nynct/c115425/202602/c00_31916768.shtml",
         "", "三、"),
        ("fujian_current_provincial_plan", "https://www.fujian.gov.cn/zwgk/zfxxgk/szfwj/jgzz/fzggwjzc/202611/t20261112_7248400.htm",
         "", "闽政办〔2026〕34号"),
    ),
    "gansu_article_boundary": (
        ("gansu_full_official_copy", "https://zsj.pingliang.gov.cn/cyzc/art/2026/art_d5e1e5ab4183477bb85294c3153b18e4.html",
         "", "甘政发〔2026〕1号"),
    ),
    "shenzhen_ai_policy": (
        ("manufacturing_issuing_bureau", "https://gxj.sz.gov.cn/gkmlpt/content/12/12645/post_12645602.html",
         "", "供需对接活动"),
        ("manufacturing_city_archive", "https://www.sz.gov.cn/szzt2010/wgkzl/jcgk/jcygk/zdzcjc/content/post_12646745.html",
         "", "供需对接活动"),
        ("citywide_issuing_bureau", "https://gxj.sz.gov.cn/gkmlpt/content/12/12967/post_12967724.html",
         "", "国家人工智能应用中试基地"),
        ("citywide_city_archive", "https://www.sz.gov.cn/szzt2010/wgkzl/jcgk/jcygk/zdzcjc/content/post_12966980.html",
         "", "国家人工智能应用中试基地"),
    ),
}
BODY = ("div.TRS_Editor", ".trs_editor_view", "#UCAP-CONTENT", "#ozoom", "#articleContent",
        "#article-content", "#article", "#content", ".content", ".article-content", ".article_content",
        ".article", ".news-content", ".content-text", ".main-text", ".text_con", ".article_con",
        ".abstract", ".abstract-text", ".word", ".pages_content", ".b12c", "#con_con", ".zwxl-article", "#mainText")
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
    result = {"case": name, "status": 200,
              "titles": [s for s in TITLE if first_text(tree, s)],
              "dates": [s for s in DATE if parse_date_zh(first_text(tree, s))],
              "bodies": bodies, "inline_papers": len(tree.css("div.paperBox"))}
    if name == "gansu_full_official_copy":
        paragraphs = complete_body_paragraphs(tree, ".content")
        endings = [i for i, p in enumerate(paragraphs) if p.endswith("营造全社会共同参与的良好氛围。")]
        if len(endings) == 1:
            suffix = paragraphs[endings[0] + 1:]
            result.update(paragraphs_after_ending=len(suffix),
                          navigation_prefix=bool(suffix and suffix[0].startswith(("下一篇:", "下一篇："))),
                          navigation_node=any(node.text(strip=True).startswith(("下一篇:", "下一篇："))
                                              for node in tree.css(".content div:last-child, .content p:last-child")))
    return result


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


def compare_qiushi_article(fetcher):
    from scraper.adapters import issue_archive
    from scraper.adapters._common import AdapterError
    from scraper.registry import load_registry

    original = fetcher.fetch(QIUSHI_INTERVIEW, [urlsplit(QIUSHI_INTERVIEW).hostname])
    archive = fetcher.fetch(QIUSHI_INTERVIEW_SNAPSHOT, [urlsplit(QIUSHI_INTERVIEW_SNAPSHOT).hostname])
    result = {"case": "qiushi_archived_interview", "publisher_status": original.status,
              "archive_status": archive.status, "robots_blocked": fetcher.stats["robots_blocked"],
              "complete_match": False}
    if not (original.ok and archive.ok and archive.final_url == QIUSHI_INTERVIEW_SNAPSHOT):
        return result
    entry = next(entry for entry in load_registry() if entry["id"] == "qiushi")
    try:
        current, saved = (issue_archive.parse(response.text, entry, QIUSHI_INTERVIEW)
                          for response in (original, archive))
    except AdapterError:
        result["parse_failed"] = True
        return result
    authors = {"旷思思", "尹立蒙"}
    known_edition = (current["date"] == "2026-01-01" and "如何以“场景”助力高质量发展" in current["title"]
                     and all(name in (current["author"] or "") for name in authors))
    same_metadata = bool(known_edition and all(current[key] == saved[key] for key in ("title", "date", "author")))
    same_body = current["body_paragraphs"] == saved["body_paragraphs"]
    body = current["body_paragraphs"]
    compact = ["".join(paragraph.split()).replace("：", ":") for paragraph in body]
    anchors = ("记者:", "王琛伟:", "刘冬梅:", "李晓华:")
    found_turns = sum(any(marker in paragraph for paragraph in compact) for marker in anchors)
    ending = "记者:感谢各位专家接受采访。"
    substantive = [paragraph for paragraph in compact if not paragraph.startswith(("网站编辑", "校对-", "审校-"))]
    closing = bool(substantive and substantive[-1].endswith(ending))
    complete = found_turns == len(anchors) and closing
    return {**result, "publisher_paragraphs": len(current["body_paragraphs"]),
            "archive_paragraphs": len(saved["body_paragraphs"]), "same_metadata": same_metadata,
            "same_body": same_body, "found_speakers": found_turns, "closing_verified": closing,
            "complete_match": same_metadata and same_body and complete}


def cac_roster_links(html):
    links = {month: set() for month in CAC_ROSTER_LABELS.values()}
    for anchor in clean_tree(html).css("#BodyLabel a[href]"):
        label = anchor.text(separator=" ", strip=True)
        if label not in CAC_ROSTER_LABELS:
            continue
        url = urljoin(CAC_ROSTER_INDEX, anchor.attributes["href"])
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc, parts.path, parts.fragment) != (
                "https", "www.cac.gov.cn", "/cms/pub/interact/downloadfile.jsp", ""):
            continue
        try:
            query = parse_qsl(parts.query, strict_parsing=True, max_num_fields=2, errors="strict")
        except ValueError:
            continue
        if len(query) == 2 and set(dict(query)) == {"filepath", "fText"}:
            fields = dict(query)
            if fields["filepath"] and fields["fText"] == label:
                links[CAC_ROSTER_LABELS[label]].add(url)
    return {month: next(iter(urls)) for month, urls in links.items() if len(urls) == 1}


def probe_cac_rosters(fetcher):
    host = urlsplit(CAC_ROSTER_INDEX).hostname
    page = fetcher.fetch(CAC_ROSTER_INDEX, [host])
    if not page.ok or page.final_url != CAC_ROSTER_INDEX:
        return [{"case": "cac_roster_index", "status": page.status or 0,
                 "robots_blocked": fetcher.stats["robots_blocked"]}]
    if "国家互联网信息办公室关于发布生成式人工智能服务已备案信息的公告" != first_text(
            clean_tree(page.text), "#con_title, h1"):
        return [{"case": "cac_roster_index", "status": 200, "title_verified": False}]
    links = cac_roster_links(page.text)
    results = [{"case": "cac_roster_index", "status": 200, "verified_periods": len(links)}]
    for month, url in links.items():
        row = {"case": "cac_roster_attachment", "period_end_month": month}
        results.append(row)
        if not fetcher._robots_allows(url):
            row.update(status=0, robots_blocked=True)
            continue
        fetcher._polite_wait(url)
        try:
            with fetcher._client.stream("GET", url, follow_redirects=False,
                                        headers={"Range": "bytes=0-31", "Accept-Encoding": "identity"}) as response:
                row["status"] = response.status_code
                if response.status_code in {401, 403, 429, 503}:
                    break
                if response.status_code not in {200, 206}:
                    continue
                prefix = next(response.iter_raw(chunk_size=32), b"")
                mime = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                row["mime"] = mime if MEDIA_TYPE.fullmatch(mime) else "unrecognized"
                disposition = response.headers.get("content-disposition", "").partition(";")[0].strip().lower()
                row["disposition"] = disposition if disposition in {"attachment", "inline"} else "unspecified"
                if prefix.startswith(b"%PDF-"):
                    row["format"] = "pdf"
                elif prefix.startswith(b"PK\x03\x04"):
                    row["format"] = "zip-container"
                elif prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
                    row["format"] = "office-compound-file"
                else:
                    row["format"] = "html" if mime == "text/html" else "unknown"
        except httpx.HTTPError as error:
            row.update(status=0, transport=type(error).__name__)
    return results


def main():
    case = sys.argv[1]
    archived_routes = {
        "wayback_qiushi": (QIUSHI_FIRST_ISSUE, QIUSHI_SNAPSHOT, compare_qiushi_archive),
        "wayback_qiushi_article": (QIUSHI_INTERVIEW, QIUSHI_INTERVIEW_SNAPSHOT, compare_qiushi_article),
    }
    archive = archived_routes.get(case)
    cases = CASES[case] if archive is None and case != "cac_rosters" else ()
    urls = (CAC_ROSTER_INDEX,) if case == "cac_rosters" else archive[:2] if archive else (
        url for _, url, _, _ in cases)
    domains = sorted({urlsplit(url).hostname for url in urls})
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        fetcher = Fetcher(cache_dir=Path(os.environ.get("PUBLISHER_CACHE", "/tmp/publisher-probe")),
                          transport=httpx.HTTPTransport(trust_env=False),
                          strict_html_hosts={host: {} for host in domains})
        try:
            if case == "cac_rosters":
                results = probe_cac_rosters(fetcher)
            elif archive:
                results = [archive[2](fetcher)]
            else:
                results = [inspect(fetcher, *entry, domains) for entry in cases]
        finally:
            fetcher.close()
    for row in results:
        print(json.dumps(row), flush=True)
    return int(case == "wayback_qiushi_article" and not results[0]["complete_match"])


if __name__ == "__main__":
    raise SystemExit(main())

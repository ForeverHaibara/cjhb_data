from __future__ import annotations

import json
import re
import os

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlparse, parse_qs
from tqdm import tqdm
from bs4 import BeautifulSoup

BASE_URL = "https://tieba.baidu.com"
IMG_PLACEHOLDER_SUBSTR = "icon_pc_picheader_n"


def _safe_json_loads(s: Optional[str]) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _text(tag) -> Optional[str]:
    if tag is None:
        return None
    txt = tag.get_text(" ", strip=True)
    return txt if txt else None


def _int_or_none(x) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def _parse_profile_id(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    q = parse_qs(urlparse(href).query)
    return q.get("id", [None])[0]


def _infer_page_pn_from_path(path: str) -> Optional[int]:
    m = re.search(r"pn_(\d+)", Path(path).stem)
    return int(m.group(1)) if m else None


def _resolve_image_path(
    image_url: str,
    image_url_to_path: Optional[Union[Dict[str, str], Callable[[str], Optional[str]]]],
) -> Optional[str]:
    if image_url_to_path is None:
        return None
    if callable(image_url_to_path):
        return image_url_to_path(image_url)
    return image_url_to_path.get(image_url)


def _pick_real_image_url(img_tag) -> Optional[str]:
    """
    优先顺序：
    bpic > data-original > data-src > src
    并跳过占位图标。
    """
    for attr in ("bpic", "data-original", "data-src", "src"):
        v = img_tag.get(attr)
        if not v:
            continue
        v = str(v).strip()
        if not v or v.startswith("data:"):
            continue
        if IMG_PLACEHOLDER_SUBSTR in v:
            continue
        return urljoin(BASE_URL, v)
    return None


def load_image_url_to_path_from_assets_manifest(assets_manifest_path: str) -> Dict[str, str]:
    """
    如果你在下载图片时保存了 assets_manifest.jsonl，可以用它直接建立
    image_url -> local_path 的映射。
    """
    mapping: Dict[str, str] = {}
    p = Path(assets_manifest_path)
    if not p.exists():
        return mapping

    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") != "ok":
                continue
            url = rec.get("image_url")
            path = rec.get("local_path")
            if url and path:
                mapping[str(url)] = str(path)
    return mapping


def parse_tieba_forum_html_files(
    html_paths: List[str],
    image_url_to_path: Optional[Union[Dict[str, str], Callable[[str], Optional[str]]]] = None,
) -> List[dict]:
    """
    输入一组贴吧首页 HTML 文件路径，输出帖子记录列表。

    返回每条记录的主要字段：
      - source_html_path
      - page_pn
      - thread_index_in_page
      - tid
      - first_post_id
      - title
      - thread_url
      - reply_num
      - create_time
      - summary
      - author{...}
      - last_reply{...}
      - declared_image_count
      - image_count
      - images[{image_index, image_url, image_path}]
    """
    records: List[dict] = []

    for html_path in tqdm(html_paths):
        html_path = str(html_path)
        page_pn = _infer_page_pn_from_path(html_path)

        html = Path(html_path).read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml") if "lxml" in BeautifulSoup.__module__ else BeautifulSoup(html, "html.parser")

        thread_items = soup.select("li.thread_item_box")
        for idx, li in enumerate(thread_items, start=1):
            li_data = _safe_json_loads(li.get("data-field"))

            # 标题和链接
            title_a = li.select_one("div.threadlist_title a.j_th_tit[href]")
            if not title_a:
                continue
            title = _text(title_a)
            thread_url = urljoin(BASE_URL, title_a.get("href", "").strip())

            # 主帖子/线程 id
            tid = _int_or_none(li.get("data-tid")) or _int_or_none(li_data.get("id"))
            first_post_id = _int_or_none(li_data.get("first_post_id"))
            reply_num = _int_or_none(_text(li.select_one("span.threadlist_rep_num"))) or _int_or_none(li_data.get("reply_num"))
            create_time = _text(li.select_one("span.is_show_create_time"))
            summary = _text(li.select_one("div.threadlist_abs"))

            # 作者信息（主题作者）
            author_span = li.select_one("div.threadlist_author span.tb_icon_author")
            author_a = li.select_one("div.threadlist_author a.frs-author-name.j_user_card[href]")
            author_span_data = _safe_json_loads(author_span.get("data-field") if author_span else None)
            author_a_data = _safe_json_loads(author_a.get("data-field") if author_a else None)

            author = {
                "account_name": li_data.get("author_name"),              # li 的 data-field 里常见
                "nickname": li_data.get("author_nickname"),             # li 的 data-field 里常见
                "portrait": li_data.get("author_portrait"),
                "display_name": _text(author_a),                        # 页面上直接显示的名字
                "profile_id": author_a_data.get("id") or _parse_profile_id(author_a.get("href") if author_a else None),
                "un": author_a_data.get("un"),
                "title": author_span.get("title") if author_span else None,
                "user_id": author_span_data.get("user_id"),
            }

            # 最后回复信息
            replyer_span = li.select_one("span.tb_icon_author_rely.j_replyer")
            replyer_a = li.select_one("span.tb_icon_author_rely a.frs-author-name.j_user_card[href]")
            replyer_a_data = _safe_json_loads(replyer_a.get("data-field") if replyer_a else None)

            last_reply = {
                "display_name": _text(replyer_a),
                "profile_id": replyer_a_data.get("id") or _parse_profile_id(replyer_a.get("href") if replyer_a else None),
                "un": replyer_a_data.get("un"),
                "title": replyer_span.get("title") if replyer_span else None,
                "reply_time": _text(li.select_one("span.threadlist_reply_date")),
            }

            # 图片：一个帖子可能有多张首楼图片
            declared_image_count = None
            pic_num = _text(li.select_one("div.small_pic_num"))
            if pic_num:
                m = re.search(r"(\d+)", pic_num)
                if m:
                    declared_image_count = int(m.group(1))

            images = []
            seen = set()
            for img_idx, img in enumerate(li.select("ul.threadlist_media img"), start=1):
                image_url = _pick_real_image_url(img)
                if not image_url:
                    continue
                if image_url in seen:
                    continue
                seen.add(image_url)

                image_path = _resolve_image_path(image_url, image_url_to_path)
                images.append({
                    "image_index": img_idx,
                    "image_url": image_url,
                    "image_path": image_path,
                    "attr": img.get("attr"),
                })

            record = {
                "source_html_path": html_path,
                "page_pn": page_pn,
                "thread_index_in_page": idx,
                "tid": tid,
                "first_post_id": first_post_id,
                "title": title,
                "thread_url": thread_url,
                "reply_num": reply_num,
                "create_time": create_time,
                "summary": summary,
                "author": author,
                "last_reply": last_reply,
                "declared_image_count": declared_image_count,
                "image_count": len(images),
                "images": images,
                # 下面这些 raw 字段如果你想进一步调试或复原，也可以保留；
                # 如果你想极简化存储，可以直接删掉。
                "raw": {
                    "li_data_field": li_data,
                    "data_tid": li.get("data-tid"),
                    "data_thread_type": li.get("data-thread-type"),
                },
            }
            records.append(record)

    return records

def parse_data(data):
    tid = str(data.get('tid',''))
    title = data.get('title','')
    author = data.get('author',{}).get('account_name','')
    text = data.get('summary','') or ''
    create_time = data.get('create_time','')
    try:
        year = create_time.split('-')[0]
        if len(year)<4:
            current_year = datetime.now().year
            create_time = str(current_year) + '-' + create_time
    except:
        pass
    reply_num = int(data.get('reply_num',0))
    images = [_.get('image_path','').split('images')[-1] for _ in data.get('images',[])]
    return tid,title,author,text,create_time,reply_num,images


work_dir = os.path.dirname(os.path.abspath(__file__))
html_dir = os.path.join(work_dir, 'tieba_pure_geometry_dump')
image_map = load_image_url_to_path_from_assets_manifest(f"{html_dir}\\assets_manifest.jsonl")
files = [os.path.join(html_dir,'pages',_) for _ in os.listdir(html_dir+'\\pages') if _.endswith('.html')]
posts = parse_tieba_forum_html_files(
    files,
    image_url_to_path=image_map,
) # 30 seconds
print('Extracted posts num =', len(posts))

data = [parse_data(_) for _ in posts]
print('Parsed data num =', len(data))

save_dir = os.path.join(work_dir, 'cjhb_data', 'cjhb_data.jsonl')
with open(save_dir,'w',encoding='utf-8') as f:
    for _ in data:
        f.write(json.dumps(_,ensure_ascii=False)+'\n')

print('Data saved to', save_dir)

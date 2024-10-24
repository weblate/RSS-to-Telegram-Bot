from __future__ import annotations
from typing import Union, Optional
from collections.abc import Sequence

import asyncio
from datetime import datetime
from bs4 import BeautifulSoup
from bs4.element import Tag

from src import db, web
from src.i18n import i18n
from ..utils import logger, escape_html
from .utils import get_hash, update_interval, list_sub, get_http_caching_headers, filter_urls

with open('src/opml_template.opml', 'r') as __template:
    OPML_TEMPLATE = __template.read()


async def sub(user_id: int, feed_url: str, lang: Optional[str] = None) -> dict[str, Union[int, str, db.Sub, None]]:
    ret = {'url': feed_url,
           'sub': None,
           'status': -1,
           'msg': None}

    try:
        feed = await db.Feed.get_or_none(link=feed_url)
        _sub = None
        created_new_sub = False

        if feed:
            _sub = await db.Sub.get_or_none(user=user_id, feed=feed)
        if not feed or feed.state == 0:
            wf = await web.feed_get(feed_url, lang=lang)
            rss_d = wf.rss_d
            ret['status'] = wf.status
            ret['msg'] = wf.msg
            feed_url_original = feed_url
            ret['url'] = feed_url = wf.url  # get the redirected url

            if rss_d is None:
                logger.warning(f'Sub {feed_url} for {user_id} failed')
                return ret

            if feed_url_original != feed_url:
                logger.info(f'Sub {feed_url_original} redirected to {feed_url}')
                if feed:
                    await migrate_to_new_url(feed, feed_url)

            # need to use get_or_create because we've changed feed_url to the redirected one
            feed, created_new_feed = await db.Feed.get_or_create(defaults={'title': rss_d.feed.title}, link=feed_url)
            if created_new_feed or feed.state == 0:
                feed.state = 1
                feed.error_count = 0
                feed.next_check_time = None
                http_caching_d = get_http_caching_headers(wf.headers)
                feed.etag = http_caching_d['ETag']
                feed.last_modified = http_caching_d['Last-Modified']
                feed.entry_hashes = [get_hash(entry.get('guid') or entry.get('link')) for entry in rss_d.entries]
                await feed.save()  # now we get the id
                db.effective_utils.EffectiveTasks.update(feed.id)

        if not _sub:  # create a new sub if needed
            _sub, created_new_sub = await db.Sub.get_or_create(user_id=user_id, feed=feed)
            _sub.feed = feed  # thus we don't need to fetch_related

        if not created_new_sub:
            ret['sub'] = None
            ret['msg'] = 'ERROR: ' + i18n[lang]['already_subscribed']
            return ret

        ret['sub'] = _sub
        logger.info(f'Subed {feed_url} for {user_id}')
        return ret

    except Exception as e:
        ret['msg'] = 'ERROR: ' + i18n[lang]['internal_error']
        logger.warning(f'Sub {feed_url} for {user_id} failed: ', exc_info=e)
        return ret


async def subs(user_id: int,
               feed_urls: Sequence[str],
               lang: Optional[str] = None,
               bypass_url_filter: bool = False) \
        -> Optional[dict[str, Union[dict[str, Union[int, str, db.Sub, None]], str]]]:
    feed_urls = filter_urls(feed_urls) if not bypass_url_filter else feed_urls
    if not feed_urls:
        return None

    result = await asyncio.gather(*(sub(user_id, url, lang=lang) for url in feed_urls))

    success = tuple(sub_d for sub_d in result if sub_d['sub'])
    failure = tuple(sub_d for sub_d in result if not sub_d['sub'])

    msg = (
            (f'<b>{i18n[lang]["sub_successful"]}</b>\n' if success else '')
            + '\n'.join(f'<a href="{sub_d["sub"].feed.link}">{escape_html(sub_d["sub"].feed.title)}</a>'
                        for sub_d in success)
            + ('\n\n' if success and failure else '')
            + (f'<b>{i18n[lang]["sub_failed"]}</b>\n' if failure else '')
            + '\n'.join(f'{escape_html(sub_d["url"])} ({sub_d["msg"]})' for sub_d in failure)
    )

    ret = {'sub_d_l': result, 'msg': msg}

    return ret


async def unsub(user_id: int, feed_url: str = None, sub_id: int = None, lang: Optional[str] = None) \
        -> dict[str, Union[str, db.Sub, None]]:
    ret = {'url': feed_url,
           'sub': None,
           'msg': None}

    if (feed_url and sub_id) or not (feed_url or sub_id):
        ret['msg'] = 'ERROR: ' + i18n[lang]['internal_error']
        return ret

    try:
        if feed_url:
            feed: db.Feed = await db.Feed.get_or_none(link=feed_url)
            sub_to_delete: Optional[db.Sub] = await feed.subs.filter(user=user_id).first() if feed else None
        else:  # elif sub_id:
            sub_to_delete: db.Sub = await db.Sub.get_or_none(id=sub_id, user=user_id).prefetch_related('feed')
            feed: Optional[db.Feed] = await sub_to_delete.feed if sub_to_delete else None

        if sub_to_delete is None or feed is None:
            ret['msg'] = 'ERROR: ' + i18n[lang]['subscription_not_exist']
            return ret

        await sub_to_delete.delete()
        await update_interval(feed=feed)

        sub_to_delete.feed = feed
        ret['sub'] = sub_to_delete
        ret['url'] = feed.link
        logger.info(f'Unsubed {feed.link} for {user_id}')
        return ret

    except Exception as e:
        ret['msg'] = 'ERROR: ' + i18n[lang]['internal_error']
        logger.warning(f'Unsub {feed_url} for {user_id} failed: ', exc_info=e)
        return ret


async def unsubs(user_id: int,
                 feed_urls: Sequence[str] = None,
                 sub_ids: Sequence[int] = None,
                 lang: Optional[str] = None,
                 bypass_url_filter: bool = False) \
        -> Optional[dict[str, Union[dict[str, Union[int, str, db.Sub, None]], str]]]:
    feed_urls = filter_urls(feed_urls) if not bypass_url_filter else feed_urls
    if not (feed_urls or sub_ids):
        return None

    coroutines = (
            (tuple(unsub(user_id, feed_url=url, lang=lang) for url in feed_urls) if feed_urls else tuple())
            + (tuple(unsub(user_id, sub_id=sub_id, lang=lang) for sub_id in sub_ids) if sub_ids else tuple())
    )

    result = await asyncio.gather(*coroutines)

    success = tuple(unsub_d for unsub_d in result if unsub_d['sub'])
    failure = tuple(unsub_d for unsub_d in result if not unsub_d['sub'])

    msg = (
            (f'<b>{i18n[lang]["unsub_successful"]}</b>\n' if success else '')
            + '\n'.join(f'<a href="{sub_d["sub"].feed.link}">{escape_html(sub_d["sub"].feed.title)}</a>'
                        for sub_d in success)
            + ('\n\n' if success and failure else '')
            + (f'<b>{i18n[lang]["unsub_failed"]}</b>\n' if failure else '')
            + '\n'.join(f'{escape_html(sub_d["url"])} ({sub_d["msg"]})' for sub_d in failure)
    )

    ret = {'unsub_d_l': result, 'msg': msg}

    return ret


async def unsub_all(user_id: int, lang: Optional[str] = None) \
        -> Optional[dict[str, Union[dict[str, Union[int, str, db.Sub, None]], str]]]:
    user_sub_list = await db.Sub.filter(user=user_id)
    sub_ids = tuple(_sub.id for _sub in user_sub_list)
    return await unsubs(user_id, sub_ids=sub_ids, lang=lang)


async def export_opml(user_id: int) -> Optional[bytes]:
    sub_list = await list_sub(user_id)
    opml = BeautifulSoup(OPML_TEMPLATE, 'lxml-xml')
    create_time = Tag(name='dateCreated')
    create_time.string = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S UTC')
    opml.head.append(create_time)
    empty_flags = True
    for _sub in sub_list:
        empty_flags = False
        outline = Tag(name='outline', attrs={'text': _sub.feed.title, 'xmlUrl': _sub.feed.link})
        opml.body.append(outline)
    if empty_flags:
        return None
    logger.info('Exported feed(s).')
    return opml.prettify().encode()


async def migrate_to_new_url(feed: db.Feed, new_url: str) -> Union[bool, db.Feed]:
    """
    Migrate feed's link to new url, useful when a feed is redirected to a new url.
    :param feed:
    :param new_url:
    :return:
    """
    if feed.link == new_url:
        return False

    logger.info(f'Migrating {feed.link} to {new_url}')
    new_url_feed = await db.Feed.get_or_none(link=new_url)
    if new_url_feed is None:  # new_url not occupied
        feed.link = new_url
        await feed.save()
        return True

    # new_url has been occupied by another feed
    new_url_feed.state = 1
    new_url_feed.title = feed.title
    new_url_feed.entry_hashes = feed.entry_hashes
    new_url_feed.etag = feed.etag
    new_url_feed.last_modified = feed.last_modified
    new_url_feed.error_count = 0
    new_url_feed.next_check_time = None
    await new_url_feed.save()

    await feed.subs.all().update(feed=new_url_feed)  # migrate all subs to the new feed

    await update_interval(new_url_feed)
    await feed.delete()  # delete the old feed
    return new_url_feed

"""Commands for operating on the wiki API at https://cavesofqud.gamepedia.com/api.php

API documentation: https://cavesofqud.gamepedia.com/api.php?action=help
The API currently used in the above link is 'query'.
"""
import asyncio
import concurrent.futures
import functools
import logging
import time

from discord import Colour, Embed
from discord.ext.commands import Bot, Cog, Context, command
from fuzzywuzzy import process

from shared import config, http_session

log = logging.getLogger('bot.' + __name__)


class Wiki(Cog):
    """Search the official Caves of Qud wiki."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.title_limit = config['wiki title search limit']
        self.fulltext_limit = config['wiki fulltext search limit']
        self.url = 'https://' + config['wiki'] + '/api.php'
        self.all_titles = {}  # mapping of titles to pageids, for conversion to URLs by API
        self.all_titles_stamp = 0.0  # after self.all_titles is filled, this will be its timestamp

    async def pageids_to_urls(self, pageids: list) -> list:
        """Helper function to return a list of the full URLs for a list of existing page IDs.
        Sandbox link for this query:
        https://cavesofqud.gamepedia.com/Special:ApiSandbox#action=query&format=json&prop=info&list=&pageids=1&inprop=url
        """
        str_pageids = [str(pageid) for pageid in pageids]
        params = {'format': 'json',
                  'action': 'query',
                  'prop': 'info',
                  'inprop': 'url',
                  'pageids': '|'.join(str_pageids)}
        async with http_session.get(url=self.url, params=params) as reply:
            response = await reply.json()
        urls = [response['query']['pages'][str(pageid)]['fullurl'] for pageid in pageids]
        return urls

    async def read_titles(self, namespace):
        """Helper function to read all the page titles from a single namespace.
        Sandbox link for this query:
        https://cavesofqud.gamepedia.com/Special:ApiSandbox#action=query&format=json&prop=&list=allpages&pageids=1&apnamespace=0&apfilterredir=nonredirects
        """
        fresh_titles = {}
        # there's a limit on how many titles we can fetch at a time (currently 5000 for bots)
        # and we may have more wiki articles than that someday, so fetch in batches
        got_all = False
        next_query_start = ''  # the article title to continue querying from, if necessary
        while not got_all:
            params = {'format': 'json',
                      'action': 'query',
                      'list': 'allpages',
                      'apfrom': next_query_start,  # is given by API if we didn't get all items
                      'apnamespace': namespace,
                      'apfilterredir': 'nonredirects',  # don't include redirects
                      'aplimit': 5000}  # how many pages to return per query # TODO: add to config
            async with http_session.get(url=self.url, params=params) as reply:
                response = await reply.json()
            new_items = response['query']['allpages']
            for item in new_items:
                title = item['title']
                # filter out some temporary Cargo tables that won't be in main namespace later
                if not title.startswith('TEMP'):
                    fresh_titles[title] = item['pageid']
            if 'continue' in response:
                next_query_start = response['continue']['apcontinue']
            else:
                got_all = True
        return fresh_titles

    async def refresh_titles_cache(self):
        """Helper function to get, or refresh, all relevant page titles for our custom search."""
        # TODO: put cache time limit in config
        # use time.monotonic because we don't actually care what time it is, only that it ticks,
        # and can't go backwards due to time zone change/daylight savings.
        if self.all_titles != {} and time.monotonic() - self.all_titles_stamp < 900:
            # we have cached titles, and they're less than 15 minutes old
            return
        # else, we need to fetch new titles and update timestamp
        new_titles = {}
        namespaces = [0, 14]  # Main, Category  TODO: add to config
        for namespace in namespaces:
            new_titles.update(await self.read_titles(namespace))
        self.all_titles = new_titles
        self.all_titles_stamp = time.monotonic()

    @command()
    async def wiki(self, ctx: Context, *args):
        """Search titles of articles for the given text.

        The best matches for the query will be given, up to a similarity limit. The fuzzy search
        from the fuzzywuzzy package uses Levenshtein distance to determine a similarity:
        https://en.wikipedia.org/wiki/Levenshtein_distance
        up to a certain cutoff.

        The old search would allow "carbide" to return "two-handed carbide battle axe" since the
        query was in the title; the new search will still allow that, but will prioritize titles
        that are "closer" to the query first.
        """
        log.info(f'({ctx.message.channel}) <{ctx.message.author}> {ctx.message.content}')
        async with ctx.typing():
            await self.refresh_titles_cache()  # fetch, or refresh, self.all_pages
            query = ' '.join(args)
            loop = asyncio.get_running_loop()
            # run this CPU task in an executor to avoid blocking the bot in the meantime.
            # functools.partial workaround is required in order to pass keyword arguments through.
            # see: https://docs.python.org/3/library/asyncio-eventloop.html#asyncio-pass-keywords
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = await loop.run_in_executor(
                    pool,
                    functools.partial(process.extractBests,
                                      query,
                                      self.all_titles.keys(),
                                      score_cutoff=75,  # TODO: read from config
                                      limit=10))  # TODO: read from config
            if len(results) == 0:
                return await ctx.send(f'Sorry, no matches were found for that query.')
            pageids = [self.all_titles[item[0]] for item in results]  # map titles to IDs
            urls = await self.pageids_to_urls(pageids)
            reply = ''
            for title, url in zip((result[0] for result in results), urls):
                reply += f'\n[{title}]({url})'

            embed = Embed(colour=Colour(0xc3c9b1),
                          description=reply)
            await ctx.send(embed=embed)

    @command()
    async def wikisearch(self, ctx: Context, *args):
        """Search all articles for the given text.
        This command relies on the fulltext search implemented by the wiki, since downloading all
        the pages locally would be prohibitive. Since the wiki only implements one fulltext search
        algorithm we just have to use its results.
        Sandbox link for this query:
        https://cavesofqud.gamepedia.com/Special:ApiSandbox#action=query&format=json&list=search&srsearch=test&srnamespace=0%7C14&srlimit=10&srwhat=text&srprop=snippet
        """
        log.info(f'({ctx.message.channel}) <{ctx.message.author}> {ctx.message.content}')
        params = {'format': 'json',
                  'action': 'query',
                  'list': 'search',  # this parameter makes it a fulltext search
                  'srsearch': ' '.join(args),  # what to search for
                  'srnamespace': '0|14',  # 'Main' and 'Category' namespaces
                  'srwhat': 'text',  # search page body
                  'srlimit': self.fulltext_limit,  # how many pages to return
                  'srprop': 'snippet'}  # include a short preview of the match
        async with ctx.typing():
            async with http_session.get(url=self.url, params=params) as reply:
                response = await reply.json()
            if 'error' in response:
                try:
                    info = ''.join(response['error']['info'])
                    return await ctx.send(f'Sorry, that query resulted in a search error: {info}')
                except ValueError as e:
                    log.exception(e)
                    return await ctx.send(f'Sorry, that query resulted in a search error with no'
                                          ' error message. Exception logged.')
            matches = response['query']['searchinfo']['totalhits']
            if matches == 0:
                return await ctx.send(f'Sorry, no matches were found for that query.')
            results = response['query']['search']
            urls = await self.pageids_to_urls([item['pageid'] for item in results])
            reply = ''
            for num, (match, url) in enumerate(zip(results, urls), start=1):
                title = match['title']
                reply += f'[{title}]({url}): '
                snippet = match['snippet'].replace('<span class="searchmatch">', '**')
                snippet = snippet.replace('</span>', '**')
                reply += snippet + '\n'
            embed = Embed(colour=Colour(0xc3c9b1),
                          description=reply)
            await ctx.send(embed=embed)

import collections
import datetime
import itertools
import re
import textwrap
import statistics
import urllib.parse
from enum import Flag
from ipaddress import IPv4Address
from operator import attrgetter, itemgetter, methodcaller
from typing import List, Dict, Optional, Match, Any, Tuple, TextIO, cast, Sequence, Set, TypeVar, Iterable, Callable, \
    ClassVar

from abstract_configuration import SESSION_INFO, AbstractSessionInfo, AbstractConfiguration, PatternRegistry
from ip_to_host_converter import IpToHostConverter
from jinga_environment import JINJA_ENVIRONMENT
from log_entry import LogEntry
from log_parser import Session, HostInfo
from opus import slug
from opus.configuration_flags import InfoFlags, ActionFlags
from opus.query_handler import QueryHandler, MetadataSlugInfo
from opus.slug import Info


class Configuration(AbstractConfiguration):
    """
    A generator class for creating a SessionInfo.
    """
    _slug_map: slug.ToInfoMap
    _default_column_slug_info: MetadataSlugInfo
    _api_host_url: str
    _debug_show_all: bool
    _no_sessions: bool
    _ip_to_host_converter: IpToHostConverter

    DEFAULT_COLUMN_INFO = 'opusid,instrument,planet,target,time1,observationduration'.split(',')

    def __init__(self, *, api_host_url: str, debug_show_all: bool, no_sessions: bool,
                 ip_to_host_converter: IpToHostConverter,
                 **_: Any):
        self._slug_map = slug.ToInfoMap(api_host_url)
        self._default_column_slug_info = QueryHandler.get_metadata_slug_info(self.DEFAULT_COLUMN_INFO, self._slug_map)
        self._api_host_url = api_host_url
        self._debug_show_all = debug_show_all
        self._no_sessions = no_sessions
        self._ip_to_host_converter = ip_to_host_converter

    def create_session_info(self, uses_html: bool = False) -> 'SessionInfo':
        """Create a new SessionInfo"""
        return SessionInfo(self._slug_map, self._default_column_slug_info, self._debug_show_all, uses_html)

    def create_batch_html_generator(self, host_infos_by_ip: List[HostInfo]) -> 'TemplateInfo':
        return TemplateInfo(self, host_infos_by_ip)

    @property
    def api_host_url(self) -> str:
        return self._api_host_url

    @property
    def no_sessions(self) -> bool:
        return self._no_sessions

    def show_summary(self, sessions: List[Session], output: TextIO) -> None:
        all_info: Dict[str, Dict[str, bool]] = collections.defaultdict(dict)
        for session in sessions:
            session_info = cast(SessionInfo, session.session_info)
            search_slug_info, column_slug_info, _ = session_info.get_slug_info()
            for info_type, slug_and_flags in (("search", search_slug_info), ("column", column_slug_info)):
                for slug, is_obsolete in slug_and_flags:
                    all_info[info_type][slug] = is_obsolete

        def show_info(info_type: str) -> None:
            result = ', '.join(
                # Use ~ as a non-breaking space for textwrap.  We replace it with a space, below
                (slug + '~[OBSOLETE]') if all_info[slug] else slug
                for slug in sorted(all_info[info_type], key=str.lower))
            wrapped = textwrap.fill(result, 100,
                                    initial_indent=f'{info_type.title()} slugs: ', subsequent_indent='    ')
            print(wrapped.replace('~', ' '), file=output)

        show_info('search')
        print('', file=output)
        show_info('column')


class SessionInfo(AbstractSessionInfo):
    """
    A class that keeps track of information about the current user session and parses log entries based on information
    that it already knows about this session.

    This is an abstract class.  The user should only get instances of this class from the Configuration.
    """
    _session_search_slugs: Dict[str, slug.Info]
    _session_metadata_slugs: Dict[str, slug.Info]
    _session_sort_slugs_list: Set[Tuple[slug.Info, ...]]
    _help_files: Set[str]
    _downloads: List[Tuple[str, int]]
    _product_types: Set[str]
    _action_flags: ActionFlags
    _info_flags: InfoFlags
    _previous_product_info_type: Optional[List[str]]
    _query_handler: QueryHandler
    _show_all: bool

    pattern_registry = PatternRegistry()
    _downloads_sessionless: ClassVar[List[Tuple[str, LogEntry]]] = []

    def __init__(self, slug_map: slug.ToInfoMap, default_column_slug_info: MetadataSlugInfo,
                 show_all: bool, uses_html: bool):
        self._session_search_slugs = dict()
        self._session_metadata_slugs = dict()
        self._session_sort_slugs_list = set()
        self._help_files = set()
        self._downloads = []
        self._product_types = set()
        self._action_flags = ActionFlags(0)
        self._info_flags = InfoFlags(0)
        self._query_handler = QueryHandler(self, slug_map, default_column_slug_info, uses_html)
        self._uses_html = uses_html
        self._show_all = show_all

        # The previous value of types when downloading a collection
        self._previous_product_info_type = None

    def add_search_slug(self, slug_name: str, slug_info: slug.Info) -> None:
        self._session_search_slugs[slug_name] = slug_info
        if slug_info.flags.is_obsolete():
            self._action_flags |= ActionFlags.HAS_OBSOLETE_SLUG
            self._info_flags |= InfoFlags.HAS_OBSOLETE_SLUG

    def add_metadata_slug(self, slug: str, slug_info: slug.Info) -> None:
        self._session_metadata_slugs[slug] = slug_info
        if slug_info.flags.is_obsolete():
            self._action_flags |= ActionFlags.HAS_OBSOLETE_SLUG
            self._info_flags |= InfoFlags.HAS_OBSOLETE_SLUG

    def add_sort_slugs_list(self, slugs_list: Sequence[slug.Info]) -> None:
        self._session_sort_slugs_list.add(tuple(slugs_list))

    def changed_search_slugs(self) -> None:
        self._action_flags |= ActionFlags.HAS_SEARCH
        self._info_flags |= InfoFlags.PERFORMED_SEARCH

    def changed_metadata_slugs(self) -> None:
        self._action_flags |= ActionFlags.HAS_METADATA
        self._info_flags |= InfoFlags.CHANGED_SELECTED_METADATA

    def performed_download(self) -> None:
        self._action_flags |= ActionFlags.HAS_DOWNLOAD

    def fetched_gallery(self) -> None:
        self._action_flags |= ActionFlags.FETCHED_GALLERY

    def update_info_flags(self, flags: InfoFlags) -> None:
        self._info_flags |= flags

    def register_download(self, filename: str, size: int) -> None:
        self._downloads.append((filename, size))

    def register_product_types(self, product_types: Sequence[str]) -> None:
        self._product_types.update(product_types)

    def register_sessionless_download(self, path: str, entry: LogEntry) -> None:
        match = re.fullmatch(r"/downloads/([^/]+)", path)
        if match:
            self._downloads_sessionless.append((match.group(1), entry))

    def get_slug_info(self) -> Sequence[List[Tuple[str, bool]]]:
        def fixit(info: Dict[str, Info]) -> List[Tuple[str, bool]]:
            return [(slug, info[slug].flags.is_obsolete())
                    for slug in sorted(info, key=str.lower)
                    # Rob doesn't want to see slugs that start with 'qtype-' in the list.
                    if not slug.startswith('qtype-')
                    if not slug.startswith('unit-')]

        # Make a copy of session_search_slugs, and change any subgroup slugs to the base value.  If we overwrite
        # an existing value, that's fine.
        session_search_slugs = self._session_search_slugs.copy()
        for slug in self._session_search_slugs.keys():
            match = re.fullmatch(r'(.*)_\d{2,}', slug)
            if match:
                session_search_slugs[match.group(1)] = session_search_slugs.pop(slug)

        search_slug_list = fixit(session_search_slugs)
        column_slug_list = fixit(self._session_metadata_slugs)
        return search_slug_list, column_slug_list

    def get_search_names(self) -> List[str]:
        return list({value.family.label for value in self._session_search_slugs.values()})

    def get_metadata_names(self) -> List[str]:
        return list({value.family.label for value in self._session_metadata_slugs.values()})

    def get_sort_list_names(self) -> List[Tuple[str, ...]]:
        return [tuple(value.family.label for value in sort_list)
                for sort_list in self._session_sort_slugs_list]

    def get_session_flags(self) -> ActionFlags:
        return self._action_flags

    def get_info_flags(self) -> InfoFlags:
        return self._info_flags

    def get_help_files(self) -> Set[str]:
        return self._help_files

    def get_product_types(self) -> Set[str]:
        return self._product_types

    def get_downloads(self) -> List[Tuple[str, int]]:
        return self._downloads

    @classmethod
    def get_downloads_sessionless(cls) -> List[Tuple[str, LogEntry]]:
        return cls._downloads_sessionless

    def parse_log_entry(self, entry: LogEntry) -> SESSION_INFO:
        """Parses a log record within the context of the current session."""

        # We ignore all sorts of log entries.
        if entry.method != 'GET' or entry.status != 200:
            return [], None
        if entry.agent and ("bot" in entry.agent.lower() or "spider" in entry.agent.lower()):
            return [], None
        path = entry.url.path

        if path.startswith('/opus/__'):
            pass
        elif path.startswith('/downloads/'):
            self.register_sessionless_download(path, entry)
            return [], None
        else:
            return [], None

        raw_query = urllib.parse.parse_qs(entry.url.query)
        # raw_query will match a key to a list of values for that key.  Opus only uses each key once
        # (values are separated by commas), so we convert the raw query to a more useful form.
        query = {key: value[0]
                 for key, value in raw_query.items()
                 if isinstance(value, list) and len(value) == 1}
        # ignorelog is a marker to ignore this entry
        if 'ignorelog' in query:
            return [], None

        # See if the path matches one of our patterns.
        path = path[5:]  # remove '/opus'
        if path.startswith('/__fake/__'):
            path = path[7:]  # remove '/__fake
        method_and_match = self.pattern_registry.find_matching_pattern(path)
        if method_and_match:
            method, match = method_and_match
            info, reference = method(self, entry, query, match)
        else:
            info, reference = [], None
        if self._show_all and not info:
            if self._uses_html:
                info = [self.safe_format('<span class="show_all">{}</span>', path)]
            else:
                info = [f'[{path}]']
        return info, reference

    #
    # API
    #

    @pattern_registry.register(r'/__api/(data)\.json')
    @pattern_registry.register(r'/__api/(data)\.html')
    @pattern_registry.register(r'/__api/(images)\.html')
    @pattern_registry.register(r'/__api/(dataimages)\.json')
    @pattern_registry.register(r'/__api/meta/(result_count)\.json')
    def __api_data(self, _log_entry: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        return self._query_handler.handle_query(query, match.group(1))

    @pattern_registry.register(r'/__api/image/med/(.*)\.json')
    @pattern_registry.register(r'/__viewmetadatamodal/(.*)\.json')
    def __view_metadata(self,  _log_entry: LogEntry, _query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        self.update_info_flags(InfoFlags.VIEWED_SLIDE_SHOW)
        metadata = match.group(1)
        return [f'View Metadata: {metadata}'], self.__create_opus_url(metadata)

    @pattern_registry.register(r'/__api/data\.csv')
    def __download_results_csv(self, _log_entry: LogEntry, _query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        self.performed_download()
        self.update_info_flags(InfoFlags.DOWNLOADED_CSV_FILE_FOR_ALL_RESULTS)
        return ["Download CSV of Search Results"], None

    @pattern_registry.register(r'/__api/metadata_v2/(.*)\.csv')
    def __download_metadata_csv(self, log_entry: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        self.performed_download()
        self.update_info_flags(InfoFlags.DOWNLOADED_CSV_FILE_FOR_ONE_OBSERVATION)
        opus_id = match.group(1)
        self.register_download(opus_id + '.csv', log_entry.size)
        extra = 'Selected' if query.get('cols') else 'All'
        text = f'Download CSV of {extra} Metadata for OPUSID'
        if self._uses_html:
            return [self.safe_format('{}: {}', text, opus_id)], self.__create_opus_url(opus_id)
        else:
            return [f'{text}: { opus_id }'], None

    @pattern_registry.register(r'/__api/download/(.*)\.zip')
    def __download_archive(self, log_entry: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        self.performed_download()
        self.update_info_flags(InfoFlags.DOWNLOADED_ZIP_FILE_FOR_ONE_OBSERVATION)
        opus_id = match.group(1)
        self.register_download(opus_id + '.zip', log_entry.size)
        extra = 'URL' if query.get('urlonly') not in (None, "0") else 'Data'
        text = f'Download {extra} Archive for OPUSID'
        if self._uses_html:
            return [self.safe_format('{}: {}', text, opus_id)], self.__create_opus_url(opus_id)
        else:
            return [f'{text}: { opus_id }'], None

    #
    # Collections
    #

    @pattern_registry.register(r'/__collections/view\.html')
    @pattern_registry.register(r'/__cart/view\.html')
    def __collections_view_cart(self, _log_entry: LogEntry, _query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        return ['View Cart'], None

    @pattern_registry.register(r'/__collections/data\.csv')
    @pattern_registry.register(r'/__cart/data\.csv')
    def __download_cart_metadata_csv(self, _: LogEntry, _query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        self.performed_download()
        self.update_info_flags(InfoFlags.DOWNLOADED_CSV_FILE_FOR_CART)
        return ["Download CSV of Selected Metadata for Cart"], None

    @pattern_registry.register(r'/__collections/download\.(json|zip)')
    @pattern_registry.register(r'/__collections/download/default\.zip')
    @pattern_registry.register(r'/__cart/download\.json')
    def __create_archive(self, _log_entry: LogEntry, query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        self.performed_download()
        self.update_info_flags(InfoFlags.DOWNLOADED_ZIP_FILE_FOR_CART)
        has_url = query.get('urlonly') not in [None, '0']
        ptypes_field = query.get('types', None)
        ptypes = ptypes_field.split(',') if ptypes_field else []
        self.register_product_types(ptypes)
        joined_ptypes = self.quote_and_join_list(sorted(ptypes))
        text = f'Download {"URL" if has_url else "Data"} Archive for Cart: {joined_ptypes}'
        return [text], None

    # Note that the __collections/ and the __cart/ are different.
    @pattern_registry.register(r'/__collections/(view)\.json')
    @pattern_registry.register(r'/__collections/default/(view)\.json')
    @pattern_registry.register(r'/__cart/(status)\.json')
    def __download_product_types(self, _log_entry: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        if match.group(1) == 'status' and query.get('download') != '1':
            # The __cart/status version requires &download=1
            return [], None
        self.performed_download()
        ptypes_field = query.get('types', None)
        new_ptypes = ptypes_field.split(',') if ptypes_field else []
        self.register_product_types(new_ptypes)
        old_ptypes = self._previous_product_info_type
        self._previous_product_info_type = new_ptypes

        if old_ptypes is None:
            joined_new_ptypes = self.quote_and_join_list(new_ptypes)
            plural = '' if len(new_ptypes) == 1 else 's'
            return [f'Download Product Type{plural}: {joined_new_ptypes}'], None

        result = []

        def show(verb: str, items: List[str]) -> None:
            if items:
                plural = 's' if len(items) > 1 else ''
                joined_items = self.quote_and_join_list(items)
                result.append(f'{verb.title()} Product Type{plural}: {joined_items}')

        show('add', [ptype for ptype in new_ptypes if ptype not in old_ptypes])
        show('remove', [ptype for ptype in old_ptypes if ptype not in new_ptypes])

        if not result:
            result.append('Product Types are unchanged')
        return result, None

    @pattern_registry.register(r'/__collections/reset\.(html|json)')
    @pattern_registry.register(r'/__collections/default/reset\.(html|json)')
    @pattern_registry.register(r'/__cart/reset\.(html|json)')
    def __reset_cart(self, _log_entry: LogEntry, _query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        return ['Empty Cart'], None

    @pattern_registry.register(r'/__collections/(add|remove)\.json')
    @pattern_registry.register(r'/__collections/default/(add|remove)\.json')
    @pattern_registry.register(r'/__cart/(add|remove)\.json')
    def __add_remove_cart(self, _log_entry: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        opus_id = query.get('opusid') or query.get('opus_id')  # opusid is new name, opus_id is old
        selection = match.group(1).title()
        if self._uses_html and opus_id:
            return [self.safe_format('Cart {}: {}', selection.title(), opus_id)], self.__create_opus_url(opus_id)
        else:
            return [f'Cart {selection.title() + ":":<7} {opus_id or "???"}'], None

    @pattern_registry.register(r'/__collections/(add|remove)range\.json')
    @pattern_registry.register(r'/__collections/default/(add|remove)range\.json')
    @pattern_registry.register(r'/__cart/(add|remove)range\.json')
    def __add_remove_range_to_cart(self, _log: LogEntry, query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        selection = match.group(1).title()
        query_range = query.get('range', '???').replace(',', ', ')
        return [f'Cart {selection} Range: {query_range}'], None

    @pattern_registry.register(r'/__collections/addall\.json')
    @pattern_registry.register(r'/__collections/default/addall\.json')
    @pattern_registry.register(r'/__cart/addall\.json')
    def __add_all_to_cart(self, _log_entry: LogEntry, query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        query_range = query.get('range', None)
        if query_range:
            query_range = query_range.replace(',', ', ')
            return [f'Cart Add {query_range}'], None
        else:
            return [f'Cart Add All'], None

    #
    # FORMS
    #

    @pattern_registry.register(r'/__forms/column_chooser\.html')
    @pattern_registry.register(r'/__selectmetadatamodal\.json')
    def __column_chooser(self, _log_entry: LogEntry, _query: Dict[str, str], _match: Match[str]) -> SESSION_INFO:
        self.update_info_flags(InfoFlags.VIEWED_SELECT_METADATA)
        return ['Metadata Selector'], None

    #
    # INIT DETAIL
    #

    @pattern_registry.register(r'/__initdetail/(.*)\.html')
    def __initialize_detail(self, _log_entry: LogEntry, _query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        self.update_info_flags(InfoFlags.VIEWED_DETAIL_TAB)
        opus_id = match.group(1)
        if self._uses_html:
            return [self.safe_format('View Detail: {}', opus_id)], self.__create_opus_url(opus_id)
        else:
            return [f'View Detail: { opus_id }'], None

    #
    # HELP
    #

    @pattern_registry.register(r'/__help/(\w+)\.(html|pdf)')
    def __read_help_information(self, _log_entry: LogEntry, _query: Dict[str, str], match: Match[str]) -> SESSION_INFO:
        help_type, file_type = match.group(1, 2)
        help_name = help_type.upper() if help_type == 'faq' else help_type
        if help_name != 'splash':
            flag = InfoFlags.VIEWED_HELP_FILE if file_type == 'html' else InfoFlags.VIEWED_HELP_FILE_AS_PDF
            self.update_info_flags(flag)
        self._help_files.add(help_name + '.' + file_type)
        if self._uses_html:
            return [self.safe_format('Help <samp>{}</samp>', help_name)], None
        else:
            return [f'Help {help_name}'], None

    #
    # Various utilities
    #

    def __create_opus_url(self, opus_id: str) -> str:
        return self.safe_format('/opus/#/view=detail&amp;detail={0}', opus_id)


T = TypeVar('T')


class TemplateInfo:
    _configuration: Configuration
    _host_infos_by_ip:  List[HostInfo]
    _sessions: List[Session]
    _ip_to_host_name: Dict[IPv4Address, str]
    _flag_name_to_flag: Dict[str, Flag]

    def __init__(self, configuration: Configuration, host_infos_by_ip: List[HostInfo]):
        self._configuration = configuration
        self._host_infos_by_ip = host_infos_by_ip
        self._sessions = [session for host_info in host_infos_by_ip for session in host_info.sessions]
        self._ip_to_host_name = {host_info.ip: host_info.name for host_info in host_infos_by_ip if host_info.name}
        self._flag_name_to_flag = {x.name: x for x in ActionFlags}

    def generate_output(self, output: TextIO) -> None:
        template = JINJA_ENVIRONMENT.get_template('log_analysis.html')
        lines = (line.strip()
                 for chunks in template.generate(configuration=self, host_infos_by_ip=self._host_infos_by_ip)
                 for line in chunks.split('\n') if line)
        for line in lines:
            output.write(line)
            output.write("\n")

    @property
    def api_host_url(self) -> str:
        return self._configuration.api_host_url

    @property
    def ip_to_host_name(self) -> Dict[IPv4Address, str]:
        return self._ip_to_host_name

    @property
    def no_sessions(self) -> bool:
        return self._configuration.no_sessions

    def flag_name_to_flag(self, name: str) -> Flag:
        return self._flag_name_to_flag[name]

    def get_host_infos_by_date(self) -> List[Tuple[datetime.date, List[Session]]]:
        host_infos_by_time = sorted(self._sessions, key=lambda session: session.start_time(), reverse=True)
        host_infos_by_date = [(date, list(values))
                              for date, values in itertools.groupby(host_infos_by_time,
                                                                    lambda host_info: host_info.start_time().date())]
        return host_infos_by_date

    def generate_ordered_search(self) -> Sequence[Tuple[str, int, List[List[Session]]]]:
        return self.__collect_sessions_by_info(lambda si: si.get_search_names())

    def generate_ordered_metadata(self) -> Sequence[Tuple[str, int, List[List[Session]]]]:
        return self.__collect_sessions_by_info(lambda si: si.get_metadata_names())

    def generate_ordered_info_flags(self) -> Sequence[Tuple[Flag, int, List[List[Session]]]]:
        temp = cast(Iterable[InfoFlags], InfoFlags)
        return self.__collect_sessions_by_info(lambda si: si.get_info_flags().as_list(), temp)

    def generate_ordered_sort_lists(self) -> Sequence[Tuple[Tuple[str], int, List[List[Session]]]]:
        return self.__collect_sessions_by_info(methodcaller("get_sort_list_names"))

    def generate_ordered_help_files(self) -> Sequence[Tuple[str, int, List[List[Session]]]]:
        return self.__collect_sessions_by_info(methodcaller("get_help_files"))

    def generate_ordered_product_types(self) -> Sequence[Tuple[str, int, List[List[Session]]]]:
        temp = self.__collect_sessions_by_info(methodcaller("get_product_types"))
        return temp

    def generate_ordered_download_files(self) -> \
            Sequence[Tuple[str, int, List[Tuple[Optional[Session], Optional[LogEntry], int]]]]:
        info_dict: Dict[str, List[Tuple[Optional[Session], Optional[LogEntry], int]]] = collections.defaultdict(list)
        for session in self._sessions:
            session_info = cast(SessionInfo, session.session_info)
            for filename, size in session_info.get_downloads():
                info_dict[filename].append((session, None, size))
        for filename, entry in SessionInfo.get_downloads_sessionless():
            info_dict[filename].append((None, entry, entry.size))

        result: List[Tuple[str, int, List[Tuple[Optional[Session], Optional[LogEntry], int]]]] = []
        for filename, sessions_and_sizes in info_dict.items():
            sessions_and_sizes.sort(key=itemgetter(2), reverse=True)
            total_size = sum(size for _, _, size in sessions_and_sizes)
            result.append((filename, total_size, sessions_and_sizes))
        result.sort(key=itemgetter(1), reverse=True)
        return result

    def get_download_statistics(self) -> Dict[str, Any]:
        data = [size for _, size, _ in self.generate_ordered_download_files()]
        mean = int(statistics.mean(data))
        median = int(statistics.median(data))
        return dict(data=data, count=len(data), sum=sum(data), mean=mean, median=median)

    def get_session_statistics(self) -> Dict[str, Any]:
        data = [session.total_time for session in self._sessions]
        mean = statistics.mean(x.total_seconds() for x in data)
        median = statistics.median(x.total_seconds() for x in data)
        return dict(data=data,
                    count=len(data),
                    sum=sum(data, datetime.timedelta(0)),
                    mean=datetime.timedelta(round(mean)),
                    median=datetime.timedelta(round(median)))

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def __collect_sessions_by_info(self,
                                   func: Callable[[SessionInfo], Iterable[T]],
                                   fixed: Optional[Iterable[T]] = None) -> Sequence[Tuple[T, int, List[List[Session]]]]:
        info_dict: Dict[T, List[Session]] = collections.defaultdict(list)
        for session in self._sessions:
            for item in func(cast(SessionInfo, session.session_info)):
                info_dict[item].append(session)
        if fixed:
            result = [(item, len(info_dict[item]), self.__group_sessions_by_host_id(info_dict[item]))
                      for item in fixed]
        else:
            result = [(item, len(sessions), self.__group_sessions_by_host_id(sessions))
                      for item, sessions in info_dict.items()]
            result.sort(key=itemgetter(0))
            result.sort(key=itemgetter(1), reverse=True)
        return result

    def __group_sessions_by_host_id(self, sessions: List[Session]) -> List[List[Session]]:
        sessions.sort(key=lambda session: session.start_time())
        sessions.sort(key=lambda session: session.host_ip)
        grouped_sessions = [list(sessions) for _, sessions in itertools.groupby(sessions, attrgetter("host_ip"))]

        # At this point, groups are sorted by host_ip, and within each group, they are sorted by start time
        # But we want the groups sorted by length, and within length, we want them in our standard sort order
        def group_session_sorter(sessions: List[Session]) -> Tuple[int, Any]:
            host_ip = sessions[0].host_ip
            name = self._ip_to_host_name.get(host_ip)
            return -len(sessions), self.__sort_key_from_ip_and_name(host_ip, name)

        grouped_sessions.sort(key=group_session_sorter)
        return grouped_sessions

    def debug(self, arg):
        print(arg)

    @staticmethod
    def __sort_key_from_ip_and_name(ip: IPv4Address, name: Optional[str]) -> Any:
        if name:
            return 1, tuple(reversed(name.lower().split('.')))
        else:
            return 2, ip

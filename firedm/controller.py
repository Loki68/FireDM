"""
    FireDM

    multi-connections internet download manager, based on "LibCurl", and "youtube_dl".

    :copyright: (c) 2019-2021 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.

    module description:
        This is the controller module as a part of MVC design, which will replace the old application design
        in attempt to isolate logic from gui / view
        old design has gui and logic mixed together
        The Model has DownloadItem as a base class and located at model.py module
        Model and controller has an observer system where model will notify controller when changed, in turn
        controller will update the current view
"""
from datetime import datetime
import os, sys, time
from copy import copy
from threading import Thread
from queue import Queue
from datetime import date

from . import update
from .utils import *
from . import setting
from . import config
from .config import Status, MediaType
from .brain import brain
from . import video
from .video import get_media_info, process_video
from .model import ObservableDownloadItem, ObservableVideo


def set_option(**kwargs):
    """set global setting option(s) in config.py"""
    try:
        config.__dict__.update(kwargs)
        # log('Settings:', kwargs)
    except:
        pass


def get_option(key, default=None):
    """get global setting option(s) in config.py"""
    try:
        return config.__dict__.get(key, default)
    except:
        return None


def check_ffmpeg():
    """check for ffmpeg availability, first: current folder, second config.global_sett_folder,
    and finally: system wide"""

    log('check ffmpeg availability?', log_level=2)
    found = False

    # search in current app directory then default setting folder
    try:
        if config.operating_system == 'Windows':
            for folder in [config.current_directory, config.global_sett_folder]:
                for file in os.listdir(folder):
                    # print(file)
                    if file == 'ffmpeg.exe':
                        found = True
                        config.ffmpeg_actual_path = os.path.join(folder, file)
                        break
                if found:  # break outer loop
                    break
    except:
        pass

    # Search in the system
    if not found:
        cmd = 'where ffmpeg' if config.operating_system == 'Windows' else 'which ffmpeg'
        error, output = run_command(cmd, verbose=False)
        if not error:
            found = True

            # fix issue 47 where command line return \n\r with path
            output = output.strip()
            config.ffmpeg_actual_path = os.path.realpath(output)

    if found:
        log('ffmpeg checked ok! - at: ', config.ffmpeg_actual_path, log_level=2)
        return True
    else:
        log(f'can not find ffmpeg!!, install it, or add executable location to PATH, or copy executable to ',
            config.global_sett_folder, 'or', config.current_directory)


def notify(message='', title='', timeout=5, app_icon='', app_name='FireDM'):
    """
    show os notification at systray area, requires plyer (a 3rd party package)
    Note:
       When called on Windows, "app_icon" has to be a path to a file in .ICO format.

    Args:
        title(str): Title of the notification
        message(str): Message of the notification
        app_name(str): Name of the app launching this notification
        app_icon(str): Icon to be displayed along with the message,
                       note: on windows, it has to be a path to a file in .ICO format.
        timeout(int): time to display the message for, defaults to 10


    Return:
        (str): return the message argument in case of success
    """

    try:
        import plyer
        plyer.notification.notify(title=title, message=message, app_name=app_name, app_icon=app_icon, timeout=timeout)
        return message
    except Exception as e:
        log(f'plyer notification: {e}')


def write_timestamp(d):
    """write 'last modified' timestamp to downloaded file

    try to figure out the timestamp of the remote file, and if available make
    the local file get that same timestamp.

    Args:
        d (ObservableDownloadItem): download item
    """

    try:

        if d.status == Status.completed:
            # get last modified timestamp from server, example: "fri, 09 oct 2020 11:11:34 gmt"
            headers = get_headers(d.eff_url, http_headers=d.http_headers)
            timestamp = headers.get('last-modified')

            if timestamp:
                # parse timestamp, eg.      "fri, 09 oct 2020 11:11:34 gmt"
                t = time.strptime(timestamp, "%a, %d %b %Y %H:%M:%S %Z")
                t = time.mktime(t)
                log(f'writing last modified timestamp "{timestamp}" to file: {d.name}')
                os.utime(d.target_file, (t, t))

    except Exception as e:
        log('controller._write_timestamp()> error:', e)
        if config.test_mode:
            raise e


def rename(d):
    """
    rename download item
    """
    forbidden_names = os.listdir(d.folder)  # + [d.name for d in self.d_map.values()]
    d.name = auto_rename(d.name, forbidden_names)
    d.calculate_uid()

    return d


def download_simulator(d):
    print('start download simulator for id:', d.uid, d.name)

    speed = 200  # kb/s
    d.status = Status.downloading

    if d.downloaded >= d.total_size:
        d.downloaded = 0

    while True:
        time.sleep(1 / 2)
        # print(d.progress)

        d.downloaded += speed // 2 * 1024
        if d.downloaded >= d.total_size:
            d.status = Status.completed
            d.downloaded = d.total_size
            print('download simulator completed for:', d.uid, d.name)

            break

        if d.status == Status.cancelled:
            print('download simulator cancelled for:', d.uid, d.name)
            break


def download_thumbnail(d):
    """download thumbnail

    Args:
        d (ObservableDownloadItem): download item
    """

    try:
        # download thumbnail
        if d.status == Status.completed and d.thumbnail_url:
            fp = os.path.splitext(d.target_file)[0] + '.png'
            download(d.thumbnail_url, fp=fp, decode=False)

    except Exception as e:
        log('controller._download_thumbnail()> error:', e)
        if config.test_mode:
            raise e


def log_runtime_info():
    """Print useful information about the system"""
    log('-' * 20, 'FireDM', '-' * 20)

    if config.isappimage:
        release_type = 'AppImage'
    elif config.FROZEN:
        release_type = 'Frozen'
    else:
        release_type = 'Non-Frozen'

    log('Starting FireDM version:', config.APP_VERSION, release_type)
    log('operating system:', config.operating_system_info)
    log('Python version:', sys.version)
    log('current working directory:', config.current_directory)
    log('FFMPEG:', config.ffmpeg_actual_path)


def create_video_playlist(url, ytdloptions=None):
    """Process url and build video object(s) and return a video playlist"""

    log('creating video playlist', log_level=2)
    playlist = []

    try:
        info = get_media_info(url, ytdloptions=ytdloptions)

        _type = info.get('_type', 'video')

        # check results if _type is a playlist / multi_video -------------------------------------------------
        if _type in ('playlist', 'multi_video') or 'entries' in info:
            log('youtube-func()> start processing playlist')

            # videos info
            pl_info = list(info.get('entries'))  # info.get('entries') is a generator
            # log('list(info.get(entries):', pl_info)

            # create initial playlist with un-processed video objects
            for v_info in pl_info:
                v_info['formats'] = []

                # get video's url
                vid_url = v_info.get('webpage_url', None) or v_info.get('url', None) or v_info.get('id', None)

                # create video object
                vid = ObservableVideo(vid_url, v_info)

                # update info
                vid.playlist_title = info.get('title', '')
                vid.playlist_url = url

                # add video to playlist
                playlist.append(vid)

                # vid.register_callback(self.observer)
        else:

            processed_info = get_media_info(info['url'], info=info, ytdloptions=ytdloptions)

            if processed_info and processed_info.get('formats'):

                # create video object
                vid = ObservableVideo(url, processed_info)

                # get thumbnail
                vid.get_thumbnail()

                # report done processing
                vid.processed = True

                # add video to playlist
                playlist.append(vid)

                # vid.register_callback(self.observer)
            else:
                log('no video streams detected')
    except Exception as e:
        playlist = []
        log('controller._create_video_playlist:', e)
        if config.test_mode:
            raise e

    return playlist


def url_to_playlist(url, ytdloptions=None):
    d = ObservableDownloadItem()
    d.update(url)

    playlist = None

    # searching for videos
    if d.type == 'text/html' or d.size < 1024 * 1024:  # 1 MB as a max size
        playlist = create_video_playlist(url, ytdloptions=ytdloptions)

    if not playlist:
        playlist = [d]

    return playlist


class Controller:
    """controller class
     communicate with (view / gui) and has the logic for downloading process

    it will update GUI thru an update_view method "refer to view.py" by sending data when model changes
    data will be passed in key, value kwargs and must contain "command" keyword

    example:
        {command='new', 'uid': 'uid_e3345de206f17842681153dba3d28ee4', 'active': True, 'name': 'hello.mp4', ...}

    command keyword could have the value of:
        'new':              gui should create new entry in its download list
        'update':           update current download list item
        'playlist_menu':    data contains a video playlist
        'stream_menu'       data contains stream menu
        'd_list'            an item in d_list, useful for loading d_list at startup

    uid keyword:
        this is a unique id for every download item which should be used in all lookup operations

    active keyword:
        to tell if data belongs to the current active download item

    """

    def __init__(self, view_class, custom_settings={}):
        self.observer_q = Queue()  # queue to collect references for updated download items

        # youtube-dl object
        self.ydl = None

        # d_map is a dictionary that map uid to download item object
        self.d_map = {}

        self.pending_downloads_q = Queue()
        self.ignore_dlist = custom_settings.get('ignore_dlist', False)

        # load application settings
        self._load_settings()

        self.url = ''
        self.playlist = []
        self._playlist_menu = []
        self._stream_menu = []

        # create view
        self.view = view_class(controller=self)

        # observer thread, it will run in a different thread waiting on observer_q and call self._update_view
        Thread(target=self._observer, daemon=True).start()

        # import youtube-dl in a separate thread
        Thread(target=video.load_extractor_engines, daemon=True).start()

        # handle pending downloads
        Thread(target=self._pending_downloads_handler, daemon=True).start()

        # handle scheduled downloads
        Thread(target=self._scheduled_downloads_handler, daemon=True).start()

        # handle on completion actions
        Thread(target=self._on_completion_watchdog, daemon=True).start()

        # check for ffmpeg and update file path "config.ffmpeg_actual_path"
        check_ffmpeg()

    # region process url
    def auto_refresh_url(self, d):
        """refresh an expired url"""
        log('auto refresh url for:', d.name)
        url = d.url
        name = d.name
        folder = d.folder

        # refresh effective url for non video objects
        if d.type not in [MediaType.video, MediaType.audio]:
            # get headers
            headers = get_headers(url)

            eff_url = headers.get('eff_url')
            content_type = headers.get('content-type', '').split(';')[0]

            if content_type != 'text/html':
                d.eff_url = eff_url

        else:
            # process video
            playlist = create_video_playlist(url)
            if playlist:
                refreshed_d = playlist[0]

                # get old name and folder
                refreshed_d.name = name
                refreshed_d.folder = folder

                # select video stream
                try:
                    refreshed_d.select_stream(name=d.selected_quality)
                    log('selected video:    ', d.selected_quality)
                    log('New selected video:', refreshed_d.selected_quality)
                except:
                    pass

                # select audio stream
                try:
                    match = [s for s in refreshed_d.audio_streams if s.name == d.audio_quality]
                    selected_audio_stream = match[0] if match else None
                    refreshed_d.select_audio(selected_audio_stream)
                    log('selected audio:    ', d.audio_quality)
                    log('New selected audio:', refreshed_d.audio_quality)
                except:
                    pass

                # update old object
                d.__dict__.update(refreshed_d.__dict__)
                d.register_callback(self.observer)

        return d

    @threaded
    def process_url(self, url):
        """take url and return a a list of ObservableDownloadItem objects

        when a "view" call this method it should expect a playlist menu (list of names) to be passed to its update
        method,

        Examples:
            playlist_menu=['1- Nasa mission to Mars', '2- how to train your dragon', ...]
            or
            playlist_menu=[] if no video playlist

        """
        if not url:
            return

        self.url = url
        self.reset()

        playlist = []
        is_video_playlist = False

        d = ObservableDownloadItem()
        d.update(url)

        # searching for videos
        if d.type == 'text/html' or d.size < 1024 * 1024:  # 1 MB as a max size
            playlist = create_video_playlist(url)

            if playlist:
                is_video_playlist = True

        if not playlist:
            playlist = [d]

        if url == self.url:
            self.playlist = playlist

            if is_video_playlist:
                log('controller> playlist ready')
                self._update_playlist_menu([str(i + 1) + '- ' + vid.name for i, vid in enumerate(self.playlist)])
            else:
                self._update_playlist_menu([])

            if self.playlist:
                d = playlist[0]
                self.report_d(d, active=True)

        return playlist

    # endregion

    # region update view
    def observer(self, **kwargs):
        """This is an observer method which get notified when change/update properties in ObservableDownloadItem
        it should be as light as possible otherwise it will impact the whole app
        it will be registered by ObservableDownloadItem while creation"""

        self.observer_q.put(kwargs)

    def _observer(self):
        """run in a thread and update views once there is a change in any download item
        it will update gui/view only on specific time intervals to prevent flooding view with data

        example of an  item in self.observer_q: {'uid': 'fdsfsafsddsfdsfds', 'name': 'some_video.mp4', ...}
        every item must have "uid" key
        """

        buffer = {}  # key = uid, value = kwargs
        report_interval = 0.5  # sec

        while True:
            for i in range(self.observer_q.qsize()):
                item = self.observer_q.get()
                uid = item.get('uid')
                if uid:
                    buffer.setdefault(uid, item).update(**item)

            for v in buffer.values():
                self._update_view(**v)

            buffer.clear()

            time.sleep(report_interval)

    def _update_view(self, **kwargs):
        """update "view" by calling its update method"""
        # print('controller._update_view:', kwargs)
        try:
            # set default command value
            kwargs.setdefault('command', 'update')

            uid = kwargs.get('uid')
            d = self.d_map.get(uid, None)

            if d is not None:
                # readonly properties will not be reported by ObservableDownloadItem
                downloaded = kwargs.get('downloaded', None)
                if downloaded:
                    extra = {k: getattr(d, k, None) for k in ['progress', 'speed', 'eta']}
                    # print('extra:', extra)

                    kwargs.update(**extra)

            # calculate total speed
            total_speed = sum([d.speed for d in self.d_map.values() if d.status == Status.downloading])
            # print('total speed -------------------------------------------------------', size_format(total_speed))
            self.view.update_view(command='total_speed', total_speed=total_speed)

            self.view.update_view(**kwargs)
            # print('controller._update_view:', kwargs)
        except Exception as e:
            log('controller._update_view()> error, ', e)
            if config.test_mode:
                raise e

    @threaded
    def report_d(self, d=None, uid=None, video_idx=None, **kwargs):
        """notify view of all properties of a download item

        Args:
            d (ObservableDownloadItem or ObservableVideo): download item
            kwargs: key, values to be included
        """

        d = d or self.get_d(uid, video_idx)
        if not d:
            return

        properties = d.watch_list

        info = {k: getattr(d, k, None) for k in properties}

        if d in self.playlist:
            info.update(video_idx=self.playlist.index(d))

        info.update(**kwargs)

        self._update_view(**info)

    # endregion

    # region settings
    def _load_settings(self, **kwargs):
        if not self.ignore_dlist:
            # load stored setting from disk
            # setting.load_setting()

            # load d_map
            self.d_map = setting.load_d_map()

            # register observer
            for d in self.d_map.values():
                d.register_callback(self.observer)

        # # update config module with custom settings
        # config.__dict__.update(**kwargs)

    def _save_settings(self):
        if not self.ignore_dlist:
            # Save setting to disk
            # setting.save_setting()

            # save d_map
            setting.save_d_map(self.d_map)

    # endregion

    # region video
    # def _process_video_info(self, info):
    #     """process video info for a video object
    #     info: youtube-dl info dict
    #     """
    #     try:
    #         # reset abort flag
    #         config.ytdl_abort = False
    #
    #         # handle types: url and url transparent
    #         _type = info.get('_type', 'video')
    #         if _type in ('url', 'url_transparent'):
    #             info = self.ydl.extract_info(info['url'], download=False, ie_key=info.get('ie_key'), process=False)
    #
    #         # process info
    #         processed_info = self.ydl.process_ie_result(info, download=False)
    #
    #         return processed_info
    #
    #     except Exception as e:
    #         log('_process_video_info()> error:', e)
    #         if config.test_mode:
    #             raise e

    # def _create_video_playlist(self, url, ytdloptions=None):
    #     """Process url and build video object(s) and return a video playlist"""
    #     log('creating video playlist', log_level=2)
    #     playlist = []
    #
    #     # we import youtube-dl in separate thread to minimize startup time, will wait in loop until it gets imported
    #     if video.ytdl is None:
    #         log(f'loading {config.active_video_extractor} ...')
    #         while not video.ytdl:
    #             time.sleep(1)  # wait until module gets imported
    #
    #     # todo: remove this (captcha workaround) junk and add offline webpage option
    #     # override _download_webpage in Youtube-dl for captcha workaround -- experimental
    #     def download_webpage_decorator(func):
    #         # return data
    #         def newfunc(obj, *args, **kwargs):
    #             # print('-' * 20, "start download page")
    #             content = func(obj, *args, **kwargs)
    #
    #             # search for word captcha in webpage content is not enough
    #             # example webpage https://www.youtube.com/playlist?list=PLwvr71r_LHEXwKxel0_hECnTb75JHEwlf
    #
    #             if config.enable_captcha_workaround and isinstance(content, str) and 'captcha' in content:
    #                 print('-' * 20, "captcha here!!")
    #                 # get webpage offline file path from user
    #                 fp = self.view.get_offline_webpage_path()
    #
    #                 if fp is None:
    #                     log('Cancelled by user')
    #                     return content
    #
    #                 if not os.path.isfile(fp):
    #                     log('invalid file path:', fp)
    #                     return content
    #
    #                 with open(fp, 'rb') as fh:
    #                     new_content = fh.read()
    #                     encoding = video.ytdl.extractor.common.InfoExtractor._guess_encoding_from_content('', new_content)
    #                     content = new_content.decode(encoding=encoding)
    #
    #             return content
    #
    #         return newfunc
    #
    #     video.ytdl.extractor.common.InfoExtractor._download_webpage = download_webpage_decorator(
    #             video.ytdl.extractor.common.InfoExtractor._download_webpage)
    #
    #     # get global youtube_dl options
    #     options = get_ytdl_options()
    #
    #     if ytdloptions:
    #         options.update(ytdloptions)
    #
    #     self.ydl = video.ytdl.YoutubeDL(options)
    #
    #     # reset abort flag
    #     config.ytdl_abort = False
    #     try:
    #         # fetch info by youtube-dl
    #         info = self.ydl.extract_info(url, download=False, process=False)
    #
    #         # print(info)
    #
    #         # don't process direct links, youtube-dl warning message "URL could be a direct video link, returning it as such."
    #         # refer to youtube-dl/extractor/generic.py
    #         if not info or info.get('direct'):
    #             log('controller._create_video_playlist()> No streams found')
    #             return []
    #
    #         """
    #             refer to youtube-dl/extractor/generic.py
    #             _type key:
    #
    #             _type "playlist" indicates multiple videos.
    #                 There must be a key "entries", which is a list, an iterable, or a PagedList
    #                 object, each element of which is a valid dictionary by this specification.
    #                 Additionally, playlists can have "id", "title", "description", "uploader",
    #                 "uploader_id", "uploader_url" attributes with the same semantics as videos
    #                 (see above).
    #
    #             _type "multi_video" indicates that there are multiple videos that
    #                 form a single show, for examples multiple acts of an opera or TV episode.
    #                 It must have an entries key like a playlist and contain all the keys
    #                 required for a video at the same time.
    #
    #             _type "url" indicates that the video must be extracted from another
    #                 location, possibly by a different extractor. Its only required key is:
    #                 "url" - the next URL to extract.
    #                 The key "ie_key" can be set to the class name (minus the trailing "IE",
    #                 e.g. "Youtube") if the extractor class is known in advance.
    #                 Additionally, the dictionary may have any properties of the resolved entity
    #                 known in advance, for example "title" if the title of the referred video is
    #                 known ahead of time.
    #
    #             _type "url_transparent" entities have the same specification as "url", but
    #                 indicate that the given additional information is more precise than the one
    #                 associated with the resolved URL.
    #                 This is useful when a site employs a video service that hosts the video and
    #                 its technical metadata, but that video service does not embed a useful
    #                 title, description etc.
    #         """
    #         _type = info.get('_type', 'video')
    #
    #         # handle types: url and url transparent
    #         if _type in ('url', 'url_transparent'):
    #             # handle youtube user links ex: https://www.youtube.com/c/MOTORIZADO/videos
    #             # issue: https://github.com/firedm/FireDM/issues/146
    #             # info: {'_type': 'url', 'url': 'https://www.youtube.com/playlist?list=UUK32F9z7s_JhACkUdVoWdag',
    #             # 'ie_key': 'YoutubePlaylist', 'extractor': 'youtube:user', 'webpage_url': 'https://www.youtube.com/c/MOTORIZADO/videos',
    #             # 'webpage_url_basename': 'videos', 'extractor_key': 'YoutubeUser'}
    #
    #             info = self.ydl.extract_info(info['url'], download=False, ie_key=info.get('ie_key'), process=False)
    #             # print(info)
    #
    #         # check results if _type is a playlist / multi_video -------------------------------------------------
    #         if _type in ('playlist', 'multi_video') or 'entries' in info:
    #             log('youtube-func()> start processing playlist')
    #             # log('Media info:', info)
    #
    #             # videos info
    #             pl_info = list(info.get('entries'))  # info.get('entries') is a generator
    #             # log('list(info.get(entries):', pl_info)
    #
    #             # create initial playlist with un-processed video objects
    #             for v_info in pl_info:
    #                 v_info['formats'] = []
    #
    #                 # get video's url
    #                 vid_url = v_info.get('webpage_url', None) or v_info.get('url', None) or v_info.get('id', None)
    #
    #                 # create video object
    #                 vid = ObservableVideo(vid_url, v_info)
    #
    #                 # update info
    #                 vid.playlist_title = info.get('title', '')
    #                 vid.playlist_url = url
    #
    #                 # add video to playlist
    #                 playlist.append(vid)
    #
    #                 # vid.register_callback(self.observer)
    #         else:
    #
    #             processed_info = self._process_video_info(info)
    #
    #             if processed_info and processed_info.get('formats'):
    #
    #                 # create video object
    #                 vid = ObservableVideo(url, processed_info)
    #
    #                 # get thumbnail
    #                 vid.get_thumbnail()
    #
    #                 # report done processing
    #                 vid.processed = True
    #
    #                 # add video to playlist
    #                 playlist.append(vid)
    #
    #                 # vid.register_callback(self.observer)
    #             else:
    #                 log('no video streams detected')
    #     except Exception as e:
    #         playlist = []
    #         log('controller._create_video_playlist:', e)
    #         if config.test_mode:
    #             raise e
    #
    #     return playlist

    def _pre_download_process(self, d, **kwargs):
        """take a ObservableDownloadItem object and process any missing information before download
        return a processed ObservableDownloadItem object"""

        # update user preferences
        d.__dict__.update(kwargs)

        # video
        if d.type == 'video' and not d.processed:
            vid = d

            try:
                vid.busy = False

                # reset abort flag
                config.ytdl_abort = False

                # process info
                processed_info = self.ydl.process_ie_result(vid.vid_info, download=False)

                if processed_info:
                    vid.vid_info = processed_info
                    vid.refresh()

                    # get thumbnail
                    vid.get_thumbnail()

                    log('_pre_download_process()> processed url:', vid.url, log_level=3)
                    vid.processed = True
                else:
                    log('_pre_download_process()> Failed,  url:', vid.url, log_level=3)

            except Exception as e:
                log('_pre_download_process()> error:', e)
                if config.test_mode:
                    raise e

            finally:
                vid.busy = False

        return d

    def _update_playlist_menu(self, pl_menu):
        """update playlist menu and send notification to view"""
        self.playlist_menu = pl_menu
        self._update_view(command='playlist_menu', playlist_menu=pl_menu)

    @threaded
    def get_stream_menu(self, d=None, uid=None, video_idx=None):
        """update stream menu and send notification to view
        """
        vid = d or self.get_d(uid, video_idx)

        # process video
        if not vid.processed:
            process_video(vid)

        self._update_view(command='stream_menu', stream_menu=vid.stream_menu, video_idx=video_idx,
                          stream_idx=vid.stream_menu_map.index(vid.selected_stream))

    def select_stream(self, stream_idx, uid=None, video_idx=None, d=None, report=True, active=True):
        """select stream for a video in playlist menu
        stream_idx: index in stream menu
        expected notifications: info of current selected video in playlist menu
        """

        d = d or self.get_d(uid, video_idx)

        if not d:
            return

        d.select_stream(index=stream_idx)

        if report:
            self.report_d(d, active=active, stream_idx=stream_idx)

    def select_audio(self, audio_idx, uid=None, video_idx=None):
        """select audio from audio menu
        Args:
            audio_idx (int): index of audio stream
            uid: unique video uid
            video_idx (int): index of video in self.playlist
        """
        # get download item
        d = self.get_d(uid, video_idx)

        if not d or not d.audio_streams:
            return None

        selected_audio_stream = d.audio_streams[audio_idx]

        d.select_audio(selected_audio_stream)
        log('Selected audio:', selected_audio_stream)

    def set_video_backend(self, extractor):
        """select video extractor backend, e.g. youtube-dl, yt_dlp, ..."""
        self.ydl = None
        video.ytdl = None
        set_option(active_video_extractor=extractor)
        video.set_default_extractor(extractor)

    # endregion

    # region download
    def _pending_downloads_handler(self):
        """handle pending downloads, should run in a dedicated thread"""

        while True:
            active_downloads = len([d for d in self.d_map.values() if d.status in Status.active_states])
            if active_downloads < config.max_concurrent_downloads:
                d = self.pending_downloads_q.get()
                if d.status == Status.pending:
                    self.download(d, silent=True)

            time.sleep(3)

    def _scheduled_downloads_handler(self):
        """handle scheduled downloads, should run in a dedicated thread"""

        while True:
            sched_downloads = [d for d in self.d_map.values() if d.status == Status.scheduled]
            if sched_downloads:
                current_datetime = datetime.now()
                for d in sched_downloads:
                    if d.sched and datetime.fromisoformat(d.sched) <= current_datetime:
                        self.download(d, silent=True)

            time.sleep(60)

    def _pre_download_checks(self, d, silent=False):
        """do all checks required for this download

        Args:
        d: ObservableDownloadItem object
        silent: if True, hide all a warning dialogues and select default

        Returns:
            (bool): True on success, False on failure
        """

        showpopup = not silent

        if not (d or d.url):
            log('Nothing to download', start='', showpopup=showpopup)
            return False
        elif not d.type or d.type == 'text/html':
            if not silent:
                response = self.get_user_response(popup_id=1)
                if response == 'Ok':
                    d.accept_html = True
            else:
                return False

        if d.status in Status.active_states:
            log('download is already in progress for this item')
            return False

        # check unsupported protocols
        unsupported = ['f4m', 'ism']
        match = [item for item in unsupported if item in d.subtype_list]
        if match:
            log(f'unsupported protocol: \n"{match[0]}" stream type is not supported yet', start='', showpopup=showpopup)
            return False

        # check for ffmpeg availability
        if d.type in (MediaType.video, MediaType.audio, MediaType.key):
            if not check_ffmpeg():

                if not silent and config.operating_system == 'Windows':
                    res = self.get_user_response(popup_id=2)
                    if res == 'Download':
                        # download ffmpeg from github
                        self._download_ffmpeg()
                else:
                    log('FFMPEG is missing', start='', showpopup=showpopup)

                return False

        # in case of missing download folder value will fallback to current download folder
        folder = d.folder or config.download_folder

        # validate destination folder for existence and permissions
        try:
            # write test file to download folder
            test_file_path = os.path.join(folder, 'test_file_.firedm')
            # skip test in case test_file already created by another thread
            if not os.path.isfile(test_file_path):
                with open(test_file_path, 'w') as f:
                    f.write('0')
                delete_file(test_file_path)

            # update download item
            d.folder = folder
        except FileNotFoundError:
            log(f'destination folder {folder} does not exist', start='', showpopup=showpopup)
            if config.test_mode:
                raise
            return False
        except (PermissionError, OSError):
            log(f"you don't have enough permission for destination folder {folder}", start='', showpopup=showpopup)
            if config.test_mode:
                raise
            return False
        except Exception as e:
            log(f'problem in destination folder {repr(e)}', start='', showpopup=showpopup)
            if config.test_mode:
                raise e
            return False

        # validate file name
        if not d.name:
            log("File name can't be empty!!", start='', showpopup=showpopup)
            return False

        # check if file with the same name exist in destination --------------------------------------------------------
        if os.path.isfile(d.target_file):

            # auto rename option
            if config.auto_rename or silent:
                action = 'Rename'
            else:
                #  show dialogue
                action = self.get_user_response(popup_id=4)

                if action not in ('Overwrite', 'Rename'):
                    log('Download cancelled by user')
                    return False

            if action == 'Rename':
                rename(d)
                log('File with the same name exist in download folder, generate new name:', d.name)
                return self._pre_download_checks(d, silent=silent)
            elif action == 'Overwrite':
                delete_file(d.target_file)

        # search current list for previous item with same name, folder ---------------------------
        if d.uid in self.d_map:
            log('download item', d.uid, 'already in list, check resume availability')

            # get download item from the list
            d_from_list = self.d_map[d.uid]

            # if match ---> resume, else rename
            if d.total_size == d_from_list.total_size:
                log('resume is possible')
                d.downloaded = d_from_list.downloaded
            else:
                log('Rename File')
                rename(d)
                return self._pre_download_checks(d, silent=silent)

        else:  # new file
            log('fresh file download')

        # ------------------------------------------------------------------

        # warning message for non-resumable downloads
        if not d.resumable and not silent:
            res = self.get_user_response(popup_id=5)
            if res != 'Yes':
                return False

        # if above checks passed will return True
        return True

    @threaded
    def download(self, d=None, uid=None, video_idx=None, silent=False, download_later=False, **kwargs):
        """start downloading an item, it will run in a separate thread automatically unless you pass
        threaded=False option

        Args:
            d (ObservableDownloadItem): download item
            silent (bool): if True, hide all a warning dialogues and select default
            kwargs: key/value for any legit attributes in DownloadItem
        """

        showpopup = not silent

        d = d or self.get_d(uid, video_idx)
        if not d:
            log('Nothing to download', showpopup=showpopup)
            return

        # make a copy of d to prevent changes in self.playlist items
        d = copy(d)

        update_object(d, kwargs)

        try:
            pre_checks = self._pre_download_checks(d, silent=silent)

            if pre_checks:
                # update view
                self.report_d(d, command='new')

                # register observer
                d.register_callback(self.observer)

                # add to download map
                self.d_map[d.uid] = d

                if not download_later:

                    # if max concurrent downloads exceeded, this download job will be added to pending queue
                    active_downloads = len(
                        [d for d in self.d_map.values() if d.status in Status.active_states])
                    if active_downloads >= config.max_concurrent_downloads:
                        d.status = Status.pending
                        self.pending_downloads_q.put(d)
                        return

                    # retry multiple times to download and auto refresh expired url
                    for n in range(config.refresh_url_retries + 1):
                        # start brain in a separate thread
                        if config.simulator:
                            t = Thread(target=download_simulator, daemon=True, args=(d,))
                        else:
                            t = Thread(target=brain, daemon=False, args=(d,))
                        t.start()

                        # wait thread to end
                        t.join()

                        if d.status != Status.error:
                            break

                        elif n >= config.refresh_url_retries:
                            log('controller: too many connection errors', 'maybe network problem or expired link',
                                start='', sep='\n', showpopup=showpopup)
                        else:  # try auto refreshing url

                            # reset errors and change status
                            d.status = Status.refreshing_url
                            d.errors = 0

                            # update view
                            self.report_d(d)

                            # refresh url
                            self.auto_refresh_url(d)

                # update view
                self.report_d(d)

                # actions to be done after completing download
                self._post_download(d)

                # report completion
                if d.status == Status.completed:
                    if config.on_download_notification:
                        # os notification popup
                        notification = f"File: {d.name} \nsaved at: {d.folder}"
                        notify(notification, title=f'{config.APP_NAME} - Download completed')

                    log(f"File: {d.name}, saved at: {d.folder}")

        except Exception as e:
            log('download()> error:', e)
            if config.test_mode:
                raise e

    def stop_download(self, uid):
        """stop downloading
        Args:
            uid (str): unique identifier property for a download item in self.d_map
        """

        d = self.d_map.get(uid)

        if d and d.status in (*Status.active_states, Status.pending):
            d.status = Status.cancelled

    def _post_download(self, d):
        """actions required after done downloading

        Args:
            d (ObservableDownloadItem): download item
        """

        # on completion actions
        if d.status == Status.completed:
            if config.download_thumbnail:
                download_thumbnail(d)

            if config.checksum:
                log()
                log(f'Calculating MD5 and SHA256 for {d.target_file} .....')
                md5, sha256 = calc_md5_sha256(fp=d.target_file)
                log(f'MD5: {md5} - for {d.name}')
                log(f'SHA256: {sha256} - for {d.name}')

            if config.use_server_timestamp:
                write_timestamp(d)

            if d.on_completion_command:
                err, output = run_command(d.on_completion_command)
                if err:
                    log(f'error executing command: {d.on_completion_command} \n{output}')

            if d.shutdown_pc:
                d.shutdown_pc = False
                self.shutdown_pc()

    def _download_ffmpeg(self, destination=config.sett_folder):
        """download ffmpeg.exe for windows os

        Args:
            destination (str): download folder

        """

        # set download folder
        config.ffmpeg_download_folder = destination

        # first check windows 32 or 64
        import platform
        # ends with 86 for 32 bit and 64 for 64 bit i.e. Win7-64: AMD64 and Vista-32: x86
        if platform.machine().endswith('64'):
            # 64 bit link
            url = 'https://github.com/firedm/FireDM/releases/download/extra/ffmpeg_64bit.exe'
        else:
            # 32 bit link
            url = 'https://github.com/firedm/FireDM/releases/download/extra/ffmpeg_32bit.exe'

        log('downloading: ', url)

        # create a download object, will save ffmpeg in setting folder
        d = ObservableDownloadItem(url=url, folder=config.ffmpeg_download_folder)
        d.update(url)
        d.name = 'ffmpeg.exe'

        self.download(d, silent=True)

    @threaded
    def autodownload(self, url, **kwargs):
        """download file automatically without user intervention
        for video files it should download best quality, for video playlist, it will download first video
        """

        # noplaylist: fetch only the video, if the URL refers to a video and a playlist
        playlist = url_to_playlist(url, ytdloptions={'noplaylist': True})
        d = playlist[0]
        update_object(d, kwargs)

        if d.type == MediaType.video and not d.all_streams:
            process_video(d)

        # set video quality
        video_quality = kwargs.get('video_quality', None)

        if video_quality and d.type == MediaType.video:
            d.select_stream(video_quality=video_quality, prefere_mp4=kwargs.get('prefere_mp4', False))

        # download item
        self.download(d, silent=True, **kwargs)

    @threaded
    def batch_download(self, urls, **kwargs):
        urls_ = "\n".join(urls)
        log(f'Batch downloading the following urls:\n {urls_}')
        # print('Batch download options:', kwargs)

        for url in urls:
            if config.shutdown:
                print('config.shutdown is true')
                break
            self.autodownload(url, **kwargs)
            time.sleep(0.5)

    @threaded
    def download_playlist(self, selected_videos, subtitles=None, **kwargs):
        """download playlist
          Args:
              selected_videos (iterable): indexes of selected videos in self.playlist
              subtitles (dict): key=language, value=selected extension
        """
        for vid_idx in selected_videos:
            d = self.playlist[vid_idx]
            kwargs['folder'] = config.download_folder

            # add number to name
            if config.use_playlist_numbers:
                kwargs['name'] = f'{vid_idx + 1}- {d.name}'

            self.download(d, silent=True, **kwargs)
            time.sleep(0.5)

            if subtitles:
                self.download_subtitles(subtitles, d=d)

    # endregion

    # region Application update
    @threaded
    def check_for_update(self, signal_id=None, wait=False, timeout=30, **kwargs):
        """check for newer version of FireDM, youtube-dl, and yt_dlp
        Args:
            signal_id(any): signal a view when this function done
            wait(bool): wait for youtube-dl and ytdlp to load
            timeout(int): timeout for above wait in seconds
        """

        # parse youtube-dl and yt_dlp versions manually (if importing still in progress)
        if not config.youtube_dl_version:
            config.youtube_dl_version = get_pkg_version('youtube_dl')

        if not config.yt_dlp_version:
            config.yt_dlp_version = get_pkg_version('yt_dlp')

        if wait:
            c = 1
            while config.youtube_dl_version is None or config.yt_dlp_version is None:
                log('\ryoutube-dl and ytdlp still loading, please wait', '.' * c, end='')
                c += 1
                if c > timeout:
                    break
                time.sleep(1)
            log()

        info = {'firedm': {'current_version': config.APP_VERSION, 'latest_version': None},
                'youtube_dl': {'current_version': config.youtube_dl_version, 'latest_version': None},
                'yt_dlp': {'current_version': config.yt_dlp_version, 'latest_version': None},
                'awesometkinter': {'current_version': config.atk_version, 'latest_version': None},
                }

        def fetch_pypi(pkg):
            pkg_info = info[pkg]
            pkg_info['latest_version'], pkg_info['url'] = update.get_pkg_latest_version(pkg, fetch_url=True)
            log('done checking:', pkg, 'current:', pkg_info['current_version'], 'latest:', pkg_info['latest_version'])

        threads = []
        pkgs = info.keys()
        for pkg in pkgs:
            if not info[pkg]['current_version']:
                log(f'{pkg} still loading, try again')
                continue
            t = Thread(target=fetch_pypi, args=(pkg,))
            threads.append(t)
            t.start()
            time.sleep(0.1)

        for t in threads:
            t.join()

        # update
        msg = 'Check for update Status:\n\n'
        new_pkgs = []
        for pkg in pkgs:
            pkg_info = info[pkg]
            current_version = pkg_info['current_version']
            latest_version = pkg_info['latest_version']

            if current_version is None:
                msg += f'    {pkg}: still loading, try again!\n\n'
            elif latest_version is None:
                msg += f'    {pkg}: check for update .... Failed!\n\n'
            elif update.parse_version(latest_version) > update.parse_version(current_version):
                msg += f'    {pkg}: New version "{latest_version}" Found!\n\n'
                new_pkgs.append(pkg)
            else:
                msg += f'    {pkg}: up to date!\n\n'

        if new_pkgs:
            msg += 'Do you want to update now? \n'
            options = ['Update', 'Cancel']

            # show update notes for firedm
            if 'firedm' in new_pkgs:
                log('getting FireDM changelog ....')

                # download change log file
                url = 'https://github.com/firedm/FireDM/raw/master/ChangeLog.txt'
                changelog = download(url, verbose=False)

                # verify server didn't send html page
                if changelog and '<!DOCTYPE html>' not in changelog:
                    msg += '\n\n\n'
                    msg += 'FireDM Change Log:\n'
                    msg += changelog

            res = self.get_user_response(msg, options)
            if res == options[0]:
                # start updating modules
                done_pkgs = {}
                for pkg in new_pkgs:
                    pkg_info = info[pkg]
                    latest_version, url = pkg_info['latest_version'], pkg_info['url']

                    log('Installing', pkg, latest_version)
                    try:
                        success = update.update_pkg(pkg, url)
                        done_pkgs[pkg] = success

                    except Exception as e:
                        log(f'failed to update {pkg}:', e)

                msg = 'Update results:\n\n'
                for pkg, success in done_pkgs.items():
                    msg += f'{pkg} - {"Updated Successfully!" if success else "Update Failed!"}\n\n'

                if any(done_pkgs.values()):
                    msg += 'Please Restart application for update to take effect!'
                else:
                    msg += 'Update failed!!!! ... try again'

                log(msg, showpopup=True)

        else:
            log(msg, showpopup=True)

        self._update_view(command='signal', signal_id=signal_id)

        today = date.today()
        config.last_update_check = (today.year, today.month, today.day)

    @threaded
    def auto_check_for_update(self):
        """auto check for firedm update"""
        if config.check_for_update and not config.disable_update_feature:
            today = date.today()
            try:
                last_check = date(*config.last_update_check)
            except:
                last_check = today
                config.last_update_check = (today.year, today.month, today.day)

            delta = today - last_check
            if delta.days >= config.update_frequency:
                res = self.get_user_response(f'Check for FireDM update?\nLast check was {delta.days} days ago',
                                             options=['Ok', 'Cancel'])
                if res == 'Ok':
                    self.check_for_update()

    def rollback_pkg_update(self, pkg):
        try:
            run_thread(update.rollback_pkg_update, pkg, daemon=True)
        except Exception as e:
            log(f'failed to restore {pkg}:', e)

    # endregion

    # region subtitles
    def get_subtitles(self, uid=None, video_idx=None):
        """send subtitles info for view
        # subtitles stored in download item in a dictionary format
        # template: subtitles = {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'xxx', 'ext': 'xxx'}
        # Example: {'en': [{'url': 'http://x.com/s1', 'ext': 'srv1'}, {'url': 'http://x.com/s2', 'ext': 'vtt'}], 'ar': [{'url': 'https://www.youtub}, {},...]

        Returns:
            (dict): e.g. {'en': ['srt', 'vtt', ...], 'ar': ['vtt', ...], ..}}
        """

        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return

        all_subtitles = d.prepare_subtitles()

        # required format {'en': ['srt', 'vtt', ...], 'ar': ['vtt', ...], ..}
        subs = {k: [item.get('ext', 'txt') for item in v] for k, v in all_subtitles.items()}

        if subs:
            return subs

    def download_subtitles(self, subs, uid=None, video_idx=None, d=None):
        """download multiple subtitles for the same download item
        Args:
            subs (dict): language name vs extension name
            uid (str): video uid
            video_idx (int): video index in self.playlist
            d(DownloadItem): DownloadItem object.
        """

        # get download item
        d = d or self.get_d(uid, video_idx)

        if not d:
            return

        all_subtitles = d.prepare_subtitles()

        for lang, ext in subs.items():
            items_list = all_subtitles.get(lang, [])

            match = [item for item in items_list if item.get('ext') == ext]
            if match:
                item = match[-1]
                url = item.get('url')

                if url:
                    run_thread(self._download_subtitle, lang, url, ext, d)
            else:
                log('subtitle:', lang, 'Not available for:', d.name)

    def _download_subtitle(self, lang_name, url, extension, d):
        """download one subtitle file"""
        try:
            file_name = f'{os.path.splitext(d.target_file)[0]}_{lang_name}.{extension}'

            # create download item object for subtitle
            sub_d = ObservableDownloadItem()
            sub_d.name = os.path.basename(file_name)
            sub_d.folder = os.path.dirname(file_name)
            sub_d.url = d.url
            sub_d.eff_url = url
            sub_d.type = 'subtitle'
            sub_d.http_headers = d.http_headers

            # if d type is hls video will download file to check if it's an m3u8 or not
            if 'hls' in d.subtype_list:
                log('downloading subtitle', file_name)
                data = download(url, http_headers=d.http_headers)

                # check if downloaded file is an m3u8 file
                if data and '#EXT' in repr(data):  # why using repr(data) instead of data?
                    sub_d.subtype_list.append('hls')

            self.download(sub_d)

        except Exception as e:
            log('download_subtitle() error', e)

    # endregion

    # region file/folder operations
    def play_file(self, uid=None, video_idx=None):
        """open download item target file or temp file"""
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return

        fp = d.target_file if os.path.isfile(d.target_file) else d.temp_file

        open_file(fp, silent=True)

    def open_file(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return

        open_file(d.target_file)

    def open_temp_file(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return

        open_file(d.temp_file)

    def open_folder(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return

        open_folder(d.folder)

    @threaded
    def delete(self, uid):
        """delete download item from the list
        Args:
            uid (str): unique identifier property for a download item in self.d_map
        """

        d = self.d_map.pop(uid)

        d.status = Status.cancelled

        # delete files
        d.delete_tempfiles()

    # endregion

    # region get info
    def get_property(self, property_name, uid=None, video_idx=None):
        d = self.get_d(uid, video_idx)

        if not d:
            return

        return getattr(d, property_name, None)

    @threaded
    def get_d_list(self):
        """update previous download list in view"""
        log('controller.get_d_list()> sending d_list')

        buff = {'command': 'd_list', 'd_list': []}
        for d in self.d_map.values():
            properties = d.watch_list
            info = {k: getattr(d, k, None) for k in properties}
            buff['d_list'].append(info)
        self.view.update_view(**buff)

    def get_segments_progress(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return None

        return d.update_segments_progress(activeonly=False)

    def get_properties(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d:
            return 'No properties available!'

        # General properties
        text = f'UID: {d.uid} \n' \
               f'Name: {d.name} \n' \
               f'Folder: {d.folder} \n' \
               f'Progress: {d.progress}% \n' \
               f'Downloaded: {format_bytes(d.downloaded)} of {format_bytes(d.total_size)} \n' \
               f'Status: {d.status} \n' \
               f'Resumable: {d.resumable} \n' \
               f'Type: {d.type}, {", ".join(d.subtype_list)}\n' \
               f'Remaining segments: {d.remaining_parts} of {d.total_parts}\n'

        if d.type == 'video':
            text += f'Protocol: {d.protocol} \n' \
                    f'Video stream: {d.selected_quality}\n'

            if 'dash' in d.subtype_list:
                text += f'Audio stream: {d.audio_quality}\n'

        if d.status == Status.scheduled:
            text += f'Scheduled: {d.sched}'

        return text

    def get_audio_menu(self, uid=None, video_idx=None):
        """get audio menu "FOR DASH VIDEOS ONLY"
        Args:
            uid: unique video uid
            video_idx (int): index of video in self.playlist

        Returns:
            (list): list of audio streams
        """
        # get download item
        d = self.get_d(uid, video_idx)

        if not d or d.type != 'video' or not d.audio_streams or 'dash' not in d.subtype_list:
            return None

        audio_menu = [stream.name for stream in d.audio_streams]
        return audio_menu

    def get_selected_audio(self, uid=None, video_idx=None):
        """send selected audio
        Args:
            uid: unique video uid
            video_idx (int): index of video in self.playlist

        Returns:
            (str): name of selected audio streams
        """
        # get download item
        d = self.get_d(uid, video_idx)

        if not d or not d.audio_streams:
            return None

        return d.audio_stream.name

    def get_d(self, uid=None, video_idx=None):
        """get download item reference

        Args:
            uid (str): unique id for a download item
            video_idx (int): index of a video download item in self.playlist

        Returns:
            (DownloadItem): if uid and video_idx omitted it will return the first object in self.playlist
        """

        try:
            if uid:
                d = self.d_map.get(uid)
            elif video_idx:
                d = self.playlist[video_idx]
            else:
                d = self.playlist[0]
        except:
            d = None

        return d

    # endregion

    # region schedul
    def schedule_start(self, uid=None, video_idx=None, target_date=None):
        """Schedule a download item
        Args:
            target_date (datetime.datetime object): target date and time to start download
        """
        # get download item
        d = self.get_d(uid, video_idx)

        if not d or not isinstance(target_date, datetime):
            return

        # validate target date should be greater than current date
        if target_date < datetime.now():
            log('Can not Schedule something in the past', 'Please select a Schedule time greater than current time',
                showpopup=True)
            return

        log(f'Schedule {d.name} at: {target_date}')
        d.sched = target_date.isoformat(sep=' ')
        d.status = Status.scheduled

    def schedule_cancel(self, uid=None, video_idx=None):
        # get download item
        d = self.get_d(uid, video_idx)

        if not d or d.status != Status.scheduled:
            return

        log(f'Schedule for: {d.name} has been cancelled')
        d.status = Status.cancelled
        d.sched = None

    # endregion

    def interactive_download(self, url, **kwargs):
        """intended to be used with command line view and offer step by step choices to download an item"""
        playlist = url_to_playlist(url)

        d = playlist[0]

        if len(playlist) > 1:
            msg = 'The url you provided is a playlist of multi-files'
            options = ['Show playlist content', 'Cancel']
            response = self.get_user_response(msg, options)

            if response == options[1]:
                log('Cancelled by user')
                return

            elif response == options[0]:
                if len(playlist) > 50:
                    msg = f'This is a big playlist with {len(playlist)} files, \n' \
                          f'Are you sure?'
                    options = ['Continue', 'Cancel']
                    r = self.get_user_response(msg, options)
                    if r == options[1]:
                        log('Cancelled by user')
                        return

                msg = 'Playlist files names, select item to download:'
                options = [d.name for d in playlist]
                response = self.get_user_response(msg, options)

                idx = options.index(response)
                d = playlist[idx]

        # pre-download process missing information, and update user preferences
        self._pre_download_process(d, **kwargs)

        # select format if video
        if d.type == 'video':
            if not d.all_streams:
                log('no streams available')
                return

            # ffmpeg check
            if not check_ffmpeg():
                log('ffmpeg missing, abort')
                return

            msg = f'Available streams:'
            options = [f'{s.mediatype} {"video" if s.mediatype != "audio" else "only"}: {str(s)}' for s in
                       d.all_streams]
            selection = self.get_user_response(msg, options)
            idx = options.index(selection)
            d.selected_stream = d.all_streams[idx]

            if 'dash' in d.subtype_list:
                msg = f'Audio Formats:'
                options = d.audio_streams
                audio = self.get_user_response(msg, options)
                d.select_audio(audio)

        msg = f'Item: {d.name} with size {format_bytes(d.total_size)}\n'
        if d.type == 'video':
            msg += f'selected video stream: {d.selected_stream}\n'
            msg += f'selected audio stream: {d.audio_stream}\n'

        msg += 'folder:' + d.folder + '\n'
        msg += f'Start Downloading?'
        options = ['Ok', 'Cancel']
        r = self.get_user_response(msg, options)
        if r == options[1]:
            log('Cancelled by user')
            return

        # download
        self.download(d, threaded=False)
        self.report_d(d, threaded=False)

    # region on completion command / shutdown
    def _on_completion_watchdog(self):
        """a separate thread to watch when "ALL" download items are completed and execute on completion action if
        configured"""

        # make sure user started any item downloading, after setting "on-completion actions"
        trigger = False

        while True:
            if config.shutdown:
                break

            # check for "on-completion actions"
            if any((config.on_completion_command, config.shutdown_pc)):
                # check for any active download, then set the trigger
                if any([d.status in Status.active_states for d in self.d_map.values()]):
                    trigger = True

                elif trigger:
                    # check if all items are completed
                    if all([d.status == Status.completed for d in self.d_map.values()]):
                        # reset the trigger
                        trigger = False

                        # execute command
                        if config.on_completion_command:
                            run_command(config.on_completion_command)

                        # shutdown
                        if config.shutdown_pc:
                            self.shutdown_pc()
            else:
                trigger = False

            time.sleep(5)

    def scedule_shutdown(self, uid):
        """schedule shutdown after an item completed downloading"""
        d = self.get_d(uid=uid)
        if d.status == Status.completed:
            return

        d.shutdown_pc = True

    def cancel_shutdown(self, uid):
        """cancel pc shutdown scedule for an item"""
        d = self.get_d(uid=uid)
        if d.shutdown_pc:
            d.shutdown_pc = False
            log('shutdown schedule cancelled for:', d.name)

    def toggle_shutdown(self, uid):
        """set shutdown flag on/off"""
        d = self.get_d(uid=uid)
        if d.status == Status.completed:
            return
        d.shutdown_pc = not d.shutdown_pc

    def shutdown_pc(self):
        """shut down computer"""
        if config.operating_system == 'Windows':
            cmd = 'shutdown -s -t 120'
            abort_cmd = 'shutdown -a'
        else:
            # tested on pop os, but it might needs root privillage on other distros.
            cmd = 'shutdown --poweroff +2'
            abort_cmd = 'shutdown -c'

        # save settings
        self._save_settings()

        err, output = run_command(cmd)
        if err:
            log('error:', output, showpopup=True)
            return

        res = self.get_user_response('your device will shutdown after 2 minutes \n'
                                     f'{output} \n'
                                     'press "ABORT!" to cancel', options=['ABORT!'])
        if res == 'ABORT!':
            run_command(abort_cmd)
        else:
            self.view.hide()
            self.view.quit()

    def set_on_completion_command(self, uid, command):
        d = self.get_d(uid=uid)
        if d.status == Status.completed:
            return
        d.on_completion_command = command

    def get_on_completion_command(self, uid):
        d = self.get_d(uid=uid)
        return d.on_completion_command

    # endregion

    # region general
    def get_user_response(self, msg='', options=[], popup_id=None):
        """get user response from current view

        Args:
            msg(str): a message to show
            options (list): a list of options, example: ['yes', 'no', 'cancel']
            popup_id(int): popup id number in config.py

        Returns:
            (str): response from user as a selected item from "options"
        """

        if popup_id:
            popup = config.get_popup(popup_id)
            msg = popup['body']
            options = popup['options']
            if not popup['show']:
                return popup['default']

        res = self.view.get_user_response(msg, options, popup_id=popup_id)

        return res

    def run(self):
        """run current "view" main loop"""
        self.view.run()

    def quit(self):
        config.shutdown = True  # set global shutdown flag
        config.ytdl_abort = True

        # cancel all current downloads
        for d in self.d_map.values():
            self.stop_download(d.uid)

        self._save_settings()

    def reset(self):
        """reset controller and cancel ongoing operation"""
        # stop youyube-dl
        config.ytdl_abort = True
        self.playlist = []

    # endregion

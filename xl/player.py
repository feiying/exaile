# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

from xl.nls import gettext as _

import pygst
pygst.require('0.10')
import gst

import gobject

from xl import common, event, playlist, settings, xdg
from xl.providers import ProviderHandler
import random, time, os, logging, copy, threading, urllib
import urlparse

try:
    import cPickle as pickle
except:
    import pickle

logger = logging.getLogger(__name__)

class PlayQueue(playlist.Playlist):

    """
        Manages the queue of songs to be played
    """

    def __init__(self, player, location=None):
        self.current_playlist = None
        self.current_pl_track = None
        playlist.Playlist.__init__(self, name="Queue")
        self.player = player
        player.set_queue(self)
        self.stop_track = -1
        if location is not None:
            self.load_from_location(location)

    def set_current_playlist(self, playlist):
        self.current_playlist = playlist

    def set_current_pl_track(self, track):
        self.current_pl_track = track

    def peek(self):
        track = playlist.Playlist.peek(self)
        if track == None:
            if self.current_playlist:
                track = self.current_playlist.peek()
        return track

    def next(self, player=True, track=None):
        """
            Goes to the next track, either in the queue, or in the current
            playlist.  If a track is passed in, that track is played

            :param player: play the track in addition to returning it
            :param track: if passed, play this track
        """
        if not track:
            if player:
                if self.player.current == self.stop_track:
                    self.player.stop()
                    event.log_event('stop_track', self, self.stop_track)
                    self.stop_track = -1
                    return

            if not self.ordered_tracks:
                if self.current_playlist:
                    track = self.current_playlist.next()
                    self.current_playlist.current_playing = True
                    self.current_playing = False
            else:
                track = self.ordered_tracks.pop(0)
                self.current_pos = 0
                self.current_playing = True
                if self.current_playlist:
                    self.current_playlist.current_playing = False
        if player:
            if not track:
                self.player.stop()
                return
            self.player.play(track)
        return track

    def prev(self):
        track = None
        if self.player.current:
            if self.player.get_time() < 5:
                if self.current_playlist:
                    track = self.current_playlist.prev()
            else:
                track = self.player.current
        else:
            track = self.get_current()
        self.player.play(track)
        return track

    def get_current(self):
        if self.player.current and self.current_pos > 0:
            current = self.player.current
        else:
            current = playlist.Playlist.get_current(self)
            if current == None and self.current_playlist:
                current = self.current_playlist.get_current()
        return current

    def get_current_pos(self):
        return 0

    def play(self, track=None):
        """
            start playback, either from the passed track or from already 
            queued tracks
        """
        if self.player.is_playing() and not track:
            return
        if not track:
            track = self.get_current()
        if track:
            self.player.play(track)
        else:
            self.next()

    def _save_player_state(self, location):
        state = {}
        state['state'] = self.player.get_state()
        state['position'] = self.player.get_time()
        state['playtime_stamp'] = self.player.playtime_stamp
        f = open(location, 'wb')
        pickle.dump(state, f, protocol = 2)
        f.close()

    @common.threaded
    def _restore_player_state(self, location):
        if not settings.get_option("player/resume_playback", True):
            return

        try:
            f = open(location, 'rb')
            state = pickle.load(f)
            f.close()
        except:
            return

        for req in ['state', 'position', 'playtime_stamp']:
            if req not in state:
                return

        if state['state'] in ['playing', 'paused']:
            vol = self.player.get_volume()
            self.player.set_volume(0)
            self.play()
            #time.sleep(0.5) # let the player settle
                            # TODO: find a better way to handle this, is
                            # there a specific bus message we can listen for?
            if not self.player.current:
                return

            self.player.seek(state['position'])
            if state['state'] == 'paused' or \
                    settings.get_option("player/resume_paused", False):
                self.player.toggle_pause()
            self.player.set_volume(vol)
            self.player.playtime_stamp = state['playtime_stamp']


def get_player():
    pname = settings.get_option("player/engine", "normal")
    if pname == "normal":
        logger.debug(_("Normal playback engine selected."))
        return GSTPlayer
    elif pname == "gapless":
        logger.debug(_("Gapless playback engine selected."))
        return GaplessPlayer
    elif pname == "unified":
        logger.debug(_("Unified playback engine selected."))
        return UnifiedPlayer
    else:
        logger.warning("Couldn't find specified playback engine, "
                "falling back to normal.")
        return GSTPlayer


class BaseGSTPlayer(object):
    """
        base player object
    """
    def __init__(self):
        self.current = None
        self.playing = False
        self.last_position = 0
        self.queue = None
        self.playtime_stamp = None

        self.connections = []
        self.playbin = None
        self.bus = None
        self.audio_sink = None
        self.volume_control = None

        self.equalizer = None
        self.replaygain = None

        self.setup_playbin()
        self.setup_bus()
        self.setup_gst_elements()

        event.add_callback(self._on_setting_change, 'option_set')

    def setup_playbin(self):
        """
            setup the playbin to use for playback
            needs to be overridden in subclasses
        """
        raise NotImplementedError

    def setup_bus(self):
        """
            setup the gstreamer message bus and callacks
        """
        self.bus = self.playbin.get_bus()
        self.bus.add_signal_watch()
        self.bus.enable_sync_message_emission()
        self.bus.connect('message', self.on_message)

    def _on_setting_change(self, name, object, data):
        """
            handle setting change events
        """
        if 'player/volume' == data:
            self._load_volume()
        elif 'equalizer/band-' in data:
            self._load_equalizer_values()

    def _load_equalizer_values(self):
        """
            load EQ values from settings
        """
        if self.equalizer:
            for band in range(0, 10):
                value = settings.get_option("equalizer/band-%s"%band, 0.0)
                self.equalizer.set_property("band%s"%band, value)

    def _load_volume(self):
        """
            load volume from settings
        """
        volume = settings.get_option("player/volume", 1)
        self.set_volume(volume)

    def setup_gst_elements(self):
        """
            sets up additional gst elements
        """
        elements = []

        self.provided = ProviderBin("stream_element")
        elements.append(self.provided)

        self.pp = Postprocessing()
        elements.append(self.pp)


        self.audio_sink = sink_from_preset(
        settings.get_option("player/audiosink", "auto"))
        if not self.audio_sink:
            logger.warning("Could not enable %s sink, attempting to autoselect."
                    %settings.get_option("player/audiosink", "auto"))
            self.audio_sink = sink_from_preset("auto")
        self.sinks = []
        self._load_volume()
        elements.append(self.audio_sink)


        
        # join everything together into a Bin to use as the playbin's sink
        sinkbin = gst.Bin()
        sinkbin.add(*elements)
        gst.element_link_many(*elements)
        sinkpad = elements[0].get_static_pad("sink")
        sinkbin.add_pad(gst.GhostPad('sink', sinkpad))

        self.playbin.set_property("audio-sink", sinkbin)

    def tag_func(self, *args):
        event.log_event('tags_parsed', self, (self.current, args[0]))

    def eof_func(self, *args):
        """
            called at the end of a stream
            override in subclasses
        """
        raise NotImplementedError

    def set_queue(self, queue):
        """
            sets the queue object to use for playback
        """
        self.queue = queue

    def set_volume(self, vol):
        """
            sets the volume

            this does NOT save the volume to settings. modifying the volume in 
            setings however will call this automatically
        """
        self.audio_sink.set_volume(vol)

    def on_message(self, bus, message, reading_tag = False):
        """
            Called when a message is received from gstreamer
        """
        if message.type == gst.MESSAGE_TAG and self.tag_func:
            self.tag_func(message.parse_tag())
        elif message.type == gst.MESSAGE_EOS and not self.is_paused():
            self.eof_func()
        elif message.type == gst.MESSAGE_ERROR:
            logger.error("%s %s" %(message, dir(message)) )
            a = message.parse_error()[0]
            self._on_playback_error(a.message)
        elif message.type == gst.MESSAGE_BUFFERING:
            percent = message.parse_buffering()
            if percent < 100:
                self.playbin.set_state(gst.STATE_PAUSED)
            else:
                logger.info(_('Buffering complete'))
                self.playbin.set_state(gst.STATE_PLAYING)
            if percent % 5 == 0:
                event.log_event('playback_buffering', self, percent)
        return True

    def _get_gst_state(self):
        """
            Returns the raw GStreamer state
        """
        return self.playbin.get_state(timeout=50*gst.MSECOND)[1]

    def get_state(self):
        """
            Returns the player state: 'playing', 'paused', or 'stopped'.
        """
        state = self._get_gst_state()
        if state == gst.STATE_PLAYING:
            return 'playing'
        elif state == gst.STATE_PAUSED:
            return 'paused'
        else:
            return 'stopped'

    def is_playing(self):
        """
            Returns True if the player is currently playing
        """
        return self._get_gst_state() == gst.STATE_PLAYING

    def is_paused(self):
        """
            Returns True if the player is currently paused
        """
        return self._get_gst_state() == gst.STATE_PAUSED

    def get_position(self):
        """
            Gets the current playback position of the playing track
        """
        if self.is_paused(): return self.last_position
        try:
            self.last_position = \
                self.playbin.query_position(gst.FORMAT_TIME)[0]
        except gst.QueryError:
            self.last_position = 0

        return self.last_position

    def get_time(self):
        """
            Gets current playback time in seconds
        """
        return self.get_position()/gst.SECOND

    def get_progress(self):
        """
            Gets current playback progress in percent
        """
        try:
            progress = self.get_time()/float(self.current.get_duration())
        except ZeroDivisionError:
            progress = 0
        return progress

    def update_playtime(self):
        """
            updates the total playtime for the currently playing track
        """
        if self.current and self.playtime_stamp:
            last = self.current['playtime']
            if type(last) == str:
                try:
                    last = int(last)
                except:
                    last = 0
            elif type(last) != int:
                last = 0
            self.current['playtime'] = last + int(time.time() - \
                    self.playtime_stamp)
            self.playtime_stamp = None

    def reset_playtime_stamp(self):
        self.playtime_stamp = int(time.time())

    def _get_track_uri(self, track):
        uri = track.get_loc_for_io()
        split = urlparse.urlsplit(uri)
        # TODO: remove this before 0.3.0 since it is not needed for
        #   stable->stable upgrades
        assert split[0] != "", _("Exaile now uses absolute URI's, please "
                                 "delete/rename your %s directory") \
                                         % xdg.data_home
        path = common.local_file_from_url(uri).encode()
        path = urllib.pathname2url(path)
        uri = urlparse.urlunsplit(split[0:2] + (path, '', ''))
        return uri

    def __notify_source(self, *args):
        # this is for handling multiple CD devices properly
        source = self.playbin.get_property('source')
        device = self.current.get_loc_for_io().split("#")[-1]
        source.set_property('device', device)
        self.playbin.disconnect(self.notify_id)

    def _on_playback_error(self, message):
        """
            Called when there is an error during playback
        """
        event.log_event('playback_error', self, message)

    def play(self, track):
        """
            plays the specified track, overriding any currently playing track

            if the track cannot be played, playback stops completely
        """
        self.stop()

        if track is None:
            return False

        # make sure the file exists if this is supposed to be a local track
        if track.is_local():
            if not track.exists():
                logger.error(_("File does not exist: %s") % 
                    track.get_loc())
                return False
       
        self.current = track
        
        uri = self._get_track_uri(track)
        logger.info(_("Playing %s") % uri)
        self.reset_playtime_stamp()

        self.playbin.set_property("uri", uri)
        if urlparse.urlsplit(uri)[0] == "cdda":
            self.notify_id = self.playbin.connect('notify::source',
                    self.__notify_source)

        self.playbin.set_state(gst.STATE_PLAYING)
        event.log_event('playback_start', self, track)

        return True

    def stop(self):
        """
            stop playback
        """
        if self.is_playing() or self.is_paused():
            self.update_playtime()
            current = self.current
            self.playbin.set_state(gst.STATE_NULL)
            self.current = None
            event.log_event('playback_end', self, current)
            return True
        return False

    def pause(self):
        """
            pause playback. DOES NOT TOGGLE
        """
        if self.is_playing():
            self.update_playtime()
            self.playbin.set_state(gst.STATE_PAUSED)
            self.reset_playtime_stamp()
            event.log_event('playback_pause', self, self.current)
            return True
        return False
 
    def unpause(self):
        """
            unpause playback
        """
        if self.is_paused():
            self.reset_playtime_stamp()

            # gstreamer does not buffer paused network streams, so if the user
            # is unpausing a stream, just restart playback
            if not self.current.is_local():
                self.playbin.set_state(gst.STATE_READY)

            self.playbin.set_state(gst.STATE_PLAYING)
            event.log_event('playback_resume', self, self.current)
            return True
        return False

    def toggle_pause(self):
        """
            toggle playback pause state
        """
        if self.is_paused():
            self.unpause()
        else:
            self.pause()

        event.log_event('playback_toggle_pause', self, self.current)

    def seek(self, value):
        """
            seek to the given position in the current stream
        """
        value = int(gst.SECOND * value)
        event = gst.event_new_seek(1.0, gst.FORMAT_TIME,
            gst.SEEK_FLAG_FLUSH|gst.SEEK_FLAG_ACCURATE,
            gst.SEEK_TYPE_SET, value, gst.SEEK_TYPE_NONE, 0)

        res = self.playbin.send_event(event)
        if res:
            self.playbin.set_new_stream_time(0L)
        else:
            logger.debug(_("Couldn't send seek event"))

        self.last_seek_pos = value
    

class GSTPlayer(BaseGSTPlayer):
    """
        Gstreamer engine
    """
    def __init__(self):
        BaseGSTPlayer.__init__(self)

    def setup_playbin(self):
        self.playbin = gst.element_factory_make("playbin", "player")

    def eof_func(self, *args):
        self.queue.next()


class GaplessPlayer(BaseGSTPlayer):
    """
        Gstreamer engine, using playbin2 for gapless
    """
    def __init__(self):
        BaseGSTPlayer.__init__(self)

    def eof_func(self, *args):
        self.queue.next()

    def setup_playbin(self):
        self.playbin = gst.element_factory_make('playbin2', "player")
        self.playbin.connect('about-to-finish', self.on_finish)
        
    def on_finish(self, *args):
        gobject.idle_add(self._on_finish)

    def _on_finish(self, *args):
        """
            called when a track is about to finish, so we can make it gapless
        """
        # this really should be peek(), but since the 'audio-changed'
        # signal isn't functional we have to do it this way.
        next = self.queue.next()
        if next is None:
            self.stop() #does this cut off part of the track?
            return
        uri = self._get_track_uri(next)
        self.playbin.set_property('uri', uri) #playbin2 takes care of the rest


class UnifiedPlayer(object):
    def __init__(self):
        self.playing = False
        self.last_position = 0
        self.queue = None
        self.playtime_stamp = None
        self.current_stream = 1
        self.timer_id = 0

        # have to fix the caps because gst cant deal with having them change.
        # TODO: make this a preference and/or autodetect optimal based on the
        #   output device - if its a 48000hz-native chip we dont want to send it
        #   44100hz audio all the time.
        #   Or better yet, fix gst to handle changing caps :D
        self.caps = gst.Caps(
                "audio/x-raw-int, " 
                "rate=(int)44100, "
                "width=(int)16, "
                "depth=(int)16, "
                "channels=(int)2")
        self.pipe = gst.Pipeline()
        self.adder = gst.element_factory_make("adder")
        self.audio_queue = gst.element_factory_make("queue")
        self.tee = gst.element_factory_make("tee")
    
        self.streams = [None, None]
        self.pp = Postprocessing()

        self.audio_sink = sink_from_preset(
                settings.get_option("player/audiosink", "auto"))
        if not self.audio_sink:
            logger.warning("Could not enable %s sink, attempting to autoselect."
                    %settings.get_option("player/audiosink", "auto"))
            self.audio_sink = sink_from_preset("auto")
        self.sinks = []

        self._load_queue_values()
        self._setup_pipeline()
        self.setup_bus()
       

        event.add_callback(self._on_setting_change, 'option_set')

    def _load_queue_values(self):
        # queue defaults to 1 second of audio data, however this
        # means that there's a 1 second delay between the UI and
        # the audio! Thus we reset it to 1/10 of a second, which
        # is small enough to be unnoticeable while still maintaining
        # a decent buffer. This is done as a setting so users whose
        # collections are on slower media can increase it to preserve
        # gapless, at the expense of UI lag.
        self.audio_queue.set_property("max-size-time", 
                settings.get_option("player/queue_duration", 100000))

    def _setup_pipeline(self):
        self.pipe.add(
                self.adder, 
                self.audio_queue, 
                self.pp,
                self.tee, 
                self.audio_sink
                )
        self.adder.link(self.audio_queue)
        self.audio_queue.link(self.pp)
        self.pp.link(self.tee)
        self.tee.link(self.audio_sink)

    def _on_drained(self, dec, stream):
        logger.debug("%s drained"%stream.get_name())
        #if stream.track != self.current:
        #    return
        self.unlink_stream(stream)
        if not settings.get_option("player/crossfading", False):
            tr = self.queue.next(player=False)
            self.play(tr, user=False)
    
    def setup_bus(self):
        """
            setup the gstreamer message bus and callacks
        """
        self.bus = self.pipe.get_bus()
        self.bus.add_signal_watch()
        self.bus.enable_sync_message_emission()
        self.bus.connect('message', self.on_message)

    def on_message(self, bus, message, reading_tag = False):
        if message.type == gst.MESSAGE_EOS and not self.is_paused():
            print "EOS: ", message

    def _get_current(self):
        if self.streams[self.current_stream]:
            return self.streams[self.current_stream].get_current()

    current = property(_get_current)

    def set_queue(self, queue):
        """
            sets the queue object to use for playback
        """
        self.queue = queue

    def _on_setting_change(self, name, object, data):
        """
            handle setting change events
        """
        if 'player/volume' == data:
            self._load_volume()
        elif 'player/queue_duration' == data:
            self._load_queue_values()

    def _load_volume(self):
        pass # FIXME

    def set_volume(self, vol):
        self.audio_sink.set_volume(vol)

    def _get_gst_state(self):
        """
            Returns the raw GStreamer state
        """
        return self.pipe.get_state(timeout=50*gst.MSECOND)[1]

    def get_state(self):
        """
            Returns the player state: 'playing', 'paused', or 'stopped'.
        """
        state = self._get_gst_state()
        if state == gst.STATE_PLAYING:
            return 'playing'
        elif state == gst.STATE_PAUSED:
            return 'paused'
        else:
            return 'stopped'

    def is_playing(self):
        """
            Returns True if the player is currently playing
        """
        return self._get_gst_state() == gst.STATE_PLAYING

    def is_paused(self):
        """
            Returns True if the player is currently paused
        """
        return self._get_gst_state() == gst.STATE_PAUSED

    def get_position(self):
        try:
            return self.streams[self.current_stream].get_position()
        except AttributeError:
            return 0
        
    def get_time(self):
        return self.get_position()/gst.SECOND        

    def get_progress(self):
        try:
            progress = self.get_time()/float(self.current.get_duration())
        except ZeroDivisionError:
            progress = 0
        return progress

    def play(self, track, user=True):
        if not track:
            return # we cant play nothing

        logger.debug("Attmepting to play \"%s\""%track)
        next = 1-self.current_stream

        if self.streams[next]:
            self.unlink_stream(self.streams[next])

        fading = False
        duration = 0

        if user:
            if settings.get_option("player/user_fade_enabled", False):
                fading = True
                duration = settings.get_option("player/user_fade", 1000)
            else:
                self.unlink_stream(self.streams[self.current_stream])
        else:
            if settings.get_option("player/crossfading", False):
                fading = True
                duration = settings.get_option(
                        "player/crossfade_duration", 3000)
            else:
                self.unlink_stream(self.streams[self.current_stream])

        self.streams[next] = AudioStream("Stream%s"%(next), caps=self.caps)
        self.streams[next].dec.connect("drained", self._on_drained, 
                self.streams[next])

        if not self.link_stream(self.streams[next], track):
            return False

        if fading:
            self.streams[next].set_volume(0)

        self.pipe.set_state(gst.STATE_PLAYING)
        self.streams[next]._settle_flag = 1
        gobject.idle_add(self.streams[next].set_state, gst.STATE_PLAYING)
        gobject.idle_add(self._set_state, self.pipe, gst.STATE_PLAYING)

        if fading:
            timeout = int(float(duration)/float(100))
            if self.streams[next]:
                gobject.timeout_add(timeout, self._fade_stream, 
                        self.streams[next], 1)
            if self.streams[self.current_stream]:
                gobject.timeout_add(timeout, self._fade_stream, 
                        self.streams[self.current_stream], -1, True)
            if settings.get_option("player/crossfading", False):
                time = int(track.get_duration()*1000 - duration)
                gobject.timer_id = gobject.timeout_add(time, 
                        self._start_crossfade)
                
        self.current_stream = next
        event.log_event('playback_start', self, track)

        return True

    def _set_state(self, thing, state):
        ret = thing.set_state(state)
        if ret == gst.STATE_CHANGE_SUCCESS:
            return False
        else:
            return True
        
    def _fade_stream(self, stream, direction, delete=False):
        current = stream.get_volume()
        current += direction/100.0
        stream.set_volume(current)
        if delete and current < 0.01:
            self.unlink_stream(stream)
            return False
        return 0.01 <= current <= 1

    def _start_crossfade(self, *args):
        tr = self.queue.next(player=False)
        if tr is not None:
            self.play(tr, user=False)
        if self.timer_id:
            gobject.source_remove(self.timer_id)
        if tr is None:
            self.timer_id = gobject.timeout_add(
                    1000*(self.current.get_duration() - self.get_time()),
                    self.stop)
        return False

    def _reset_crossfade_timer(self):
        if self.timer_id:
            gobject.source_remove(self.timer_id)
        if not self.is_playing():
            return
        if not settings.get_option("player/crossfading", False):
            return
        duration = settings.get_option("player/crossfade_duration", 3000)
        time = int( self.current.get_duration()*1000 - \
                (self.get_time()*1000 + duration) )
        if time < duration: # start crossfade now, we're late!
            gobject.idle_add(self._start_crossfade)
        else:
            self.timer_id = gobject.timeout_add(time, self._start_crossfade)

    def unlink_stream(self, stream):
        try:
            pad = stream.get_static_pad("src").get_peer()
            stream.unlink(self.adder)
            try:
                self.adder.release_request_pad(pad)
            except TypeError:
                pass
            gobject.idle_add(stream.set_state, gst.STATE_NULL)
            try:
                self.pipe.remove(stream)
            except RemoveError:
                logger.debug("Failed to remove stream %s"%stream)
            if stream in self.streams:
                self.streams[self.streams.index(stream)] = None
            return True
        except AttributeError:
            return True
        except:
            common.log_exception(log=logger)
            return False

    def link_stream(self, stream, track):
        self.pipe.add(stream)
        stream.link(self.adder)
        if not stream.set_track(track):
            logger.error("Failed to start playing \"%s\""%track)
            self.stop()
            return False
        return True

    def stop(self):
        """
            stop playback
        """
        if self.is_playing() or self.is_paused():
            current = self.current
            self.pipe.set_state(gst.STATE_NULL)
            for stream in self.streams:           
                self.unlink_stream(stream)
            self._reset_crossfade_timer()
            event.log_event('playback_end', self, current)
            return True
        return False

    def pause(self):
        """
            pause playback. DOES NOT TOGGLE
        """
        if self.is_playing():
            self.pipe.set_state(gst.STATE_PAUSED)
            self._reset_crossfade_timer()
            event.log_event('playback_pause', self, self.current)
            return True
        return False
 
    def unpause(self):
        """
            unpause playback
        """
        if self.is_paused():
            # gstreamer does not buffer paused network streams, so if the user
            # is unpausing a stream, just restart playback
            if not self.current.is_local():
                self.pipe.set_state(gst.STATE_READY)

            self.pipe.set_state(gst.STATE_PLAYING)
            self._reset_crossfade_timer()
            event.log_event('playback_resume', self, self.current)
            return True
        return False

    def toggle_pause(self):
        """
            toggle playback pause state
        """
        if self.is_paused():
            self.unpause()
        else:
            self.pause()

    def seek(self, value):
        """
            seek to the given position in the current stream
        """
        self.streams[self.current_stream].seek(value)
        self._reset_crossfade_timer()

    

class ProviderBin(gst.Bin, ProviderHandler):
    """
        A ProviderBin is a gst.Bin that adds and removes elements from itself
        using the providers system. Providers should be a subclass of 
        gst.Element and provide the following attributes:
            name  - name to use for this element
            index - priority within the pipeline. range [0-100] integer.
                    lower numbers are higher priority, elements having the
                    same index are ordered arbitrarily.
    """
    def __init__(self, servicename, name=None):
        """
            :param servicename: the Provider name to listen for
        """
        if name:
            gst.Bin.__init__(self, name)
        else:
            gst.Bin.__init__(self)
        ProviderHandler.__init__(self, servicename)
        self.elements = {}
        self.added_elems = []
        self.srcpad = None
        self.sinkpad = None
        self.src = None
        self.sink = None
        self.setup_elements()

    def setup_elements(self):
        state = self.get_state()[1]
        if len(self.added_elems) > 0:
            self.remove(*self.added_elems)
        elems = list(self.elements.iteritems())
        elems.sort()
        if len(elems) == 0:
            elems.append(gst.element_factory_make('identity'))
        self.add(*elems)
        if len(elems) > 1:
            gst.element_link_many(*elems)       
        self.srcpad = elems[-1].get_static_pad("src")
        if self.src:
            self.src.set_target(self.srcpad)
        else:
            self.src = gst.GhostPad('src', self.srcpad)
        self.add_pad(self.src)
        self.sinkpad = elems[0].get_static_pad("sink")
        if self.sink:
            self.sink.set_target(self.sinkpad)
        else:
            self.sink = gst.GhostPad('sink', self.sinkpad)
        self.add_pad(self.sink)
        self.added_elems = elems
        self.set_state(state)

    def on_new_provider(self, provider):
        self.elements[provider.index] = \
                self.elements.get(provider.index, []) + [provider]

    def on_del_provider(self, provider):
        try:
            self.elements[provider.index].remove(provider)
        except:
            pass

class AudioStream(gst.Bin):
    def __init__(self, name, caps=None):
        gst.Bin.__init__(self, name)
        self.notify_id = None
        self.track = None
        self.playtime_stamp = None

        self.last_position = 0
        self._settle_flag = 0
        self._seek_event = threading.Event()

        self.caps = caps
        self.setup_elems()

    def setup_elems(self):
        self.dec = gst.element_factory_make("uridecodebin")
        self.audioconv = gst.element_factory_make("audioconvert")
        self.audioresam = gst.element_factory_make("audioresample")
        self.provided = ProviderBin("stream_element")
        self.capsfilter = gst.element_factory_make("capsfilter")
        self.capsfilter.set_property("caps", self.caps)
        self.vol = gst.element_factory_make("volume")
        self.add(self.dec, 
                self.audioconv, 
                self.audioresam, 
                self.provided, 
                self.capsfilter,
                self.vol)
        self.audioconv.link(self.audioresam)
        self.audioresam.link(self.capsfilter)
        self.capsfilter.link(self.provided)
        self.provided.link(self.vol)
        self.dec.connect('no-more-pads', self._dec_pad_cb, self.audioconv)

        self.src = gst.GhostPad("src", self.vol.get_static_pad("src"))
        self.add_pad(self.src)

    def _dec_pad_cb(self, dec, v):
        try:
            dec.link(v)
        except:
            pass

    def set_volume(self, vol):
        self.vol.set_property("volume", vol)

    def get_volume(self):
        return self.vol.get_property("volume")

    def set_track(self, track):
        if not track:
            return False
        if track.is_local():
            if not track.exists():
                logger.error(_("File does not exist: %s") %
                        track.get_loc())
                return False
        
        self.track = track

        uri = track.get_loc_for_io()

        logger.info(_("Playing %s") % uri)
        self.reset_playtime_stamp()
        
        self.dec.set_property("uri", uri)

        # TODO: abstract this into generic uri handling via providers
        if uri.startswith("cdda://"):
            self.notify_id = self.dec.connect('notify::source',
                    self.__notify_source)

        return True

    def __notify_source(self, *args):
        # this is for handling multiple CD devices properly
        source = self.dec.get_property('source')
        device = self.track.get_loc().split("#")[-1]
        source.set_property('device', device)
        self.dec.disconnect(self.notify_id)

    def update_playtime(self):
        """
            updates the total playtime for the currently playing track
        """
        if self.track and self.playtime_stamp:
            last = self.track['playtime']
            if type(last) == str:
                try:
                    last = int(last)
                except:
                    last = 0
            elif type(last) != int:
                last = 0
            self.track['playtime'] = last + int(time.time() - \
                    self.playtime_stamp)
            self.playtime_stamp = None

    def reset_playtime_stamp(self):
        self.playtime_stamp = int(time.time())

    def set_state(self, state):
        logger.debug("Setting state on %s %s"%(self.get_name(), state))
        self._settle_flag = 0
        if state == gst.STATE_PLAYING:
            gst.Bin.set_state(self, state)
            self._settle_state()
            self.reset_playtime_stamp()
        elif state == gst.STATE_PAUSED:
            self.update_playtime()
            gst.Bin.set_state(self, state)
            self.reset_playtime_stamp()
        else:
            self.update_playtime()
            gst.Bin.set_state(self, state)

    def _get_gst_state(self):
        """
            Returns the raw GStreamer state
        """
        return self.get_state(timeout=50*gst.MSECOND)[1]

    def is_playing(self):
        """
            Returns True if the player is currently playing
        """
        return self._get_gst_state() == gst.STATE_PLAYING

    def is_paused(self):
        """
            Returns True if the player is currently paused
        """
        return self._get_gst_state() == gst.STATE_PAUSED

    def get_current(self):
        if self.is_playing() or self.is_paused():
            return self.track
        else:
            return None

    def get_position(self):
        if self.is_paused(): 
            return self.last_position
        try:
            self.last_position = self.dec.query_position(gst.FORMAT_TIME)[0]
        except gst.QueryError:
            common.log_exception(logger)
            self.last_position = 0
        return self.last_position

    def _settle_state(self):
        self._settle_flag = 1
        gobject.idle_add(self._settle_state_sub)

    def _settle_state_sub(self):
        """
            hack to reset gstreamer states.
            TODO: find a cleaner way of doing this.
        """
        if self._settle_flag == 1 and self._get_gst_state() == gst.STATE_PAUSED:
            logger.debug("Settling state on %s."%repr(self))
            self.set_state(gst.STATE_PLAYING)
            return True
        else:
            self._settle_flag = 0
            event.log_event("stream_settled", self, None)
            return False 

    def seek(self, value):
        """
            seek to the given position in the current stream
        """
        if self._settle_flag == 1:
            event.add_callback(self._seek_delayed, "stream_settled")
            self._seek_event.clear()
            self._seek_event.wait()

        value = int(gst.SECOND * value)
        seekevent = gst.event_new_seek(1.0, gst.FORMAT_TIME,
            gst.SEEK_FLAG_FLUSH|gst.SEEK_FLAG_ACCURATE,
            gst.SEEK_TYPE_SET, value, gst.SEEK_TYPE_NONE, 0)

        self.vol.send_event(seekevent)

        self.last_seek_pos = value

    def _seek_delayed(self, type, object, value):
        """
            internal code used if seek is called before the stream is ready
        """
        if self._settle_flag == 1 or object != self:
            return 
        event.remove_callback(self._seek_delayed, type, object)
        self._seek_event.set()    


class Postprocessing(ProviderBin):
    def __init__(self):
        ProviderBin.__init__(self, 'postprocessing_element', 
                name="Postprocessing")

class BaseSink(gst.Bin):
    pass


SINK_PRESETS = {
        "auto"  : {
            "name"      : _("Automatic"), 
            "elem"      : "autoaudiosink", 
            "options"   : {},
            },
        "alsa"  : {
            "name"      : _("Alsa"),
            "elem"      : "alsasink",
            "options"   : {},
            },
        "oss"   : {
            "name"      : _("Oss"),
            "elem"      : "osssink",
            "options"   : {},
            },
        "pulse" : {
            "name"      : _("Pulseaudio"),
            "elem"      : "pulsesink",
            "options"   : {},
            },
        }

def sink_from_preset(preset):
    try:
        d = SINK_PRESETS[preset]
        sink = AudioSink(d['name'], d['elem'], d['options'])
        return sink
    except:
        return None

class AudioSink(BaseSink):
    def __init__(self, name, elem, options, *args, **kwargs):
        BaseSink.__init__(self, *args, **kwargs)
        self.name = name
        self.sink_elem = elem
        self.options = options
        self.provided = ProviderBin('sink_element')
        self.vol = gst.element_factory_make("volume")
        self.sink = gst.element_factory_make(self.sink_elem)
        elems = [self.provided, self.vol, self.sink]
        self.add(*elems)
        gst.element_link_many(*elems)
        self.sinkghost = gst.GhostPad("sink", 
                self.provided.get_static_pad("sink"))
        self.add_pad(self.sinkghost)
        self.load_options()

    def load_options(self):
        #TODO: make this reset any non-explicitly set options to default
        # this setting is a list of strings of the form "param=value"
        options = settings.get_option("player/%s_sink_options"%self.name, [])
        optdict = copy.copy(self.options)
        optdict.update(dict([v.split("=") for v in options]))
        for param, value in optdict.iteritems():
            try:
                self.sink.set_property(param, value)
            except:
                common.log_exception(log=logger)
                logger.warning(_("Could not set parameter %s for %s") % 
                        (param, self.sink_elem))

    def set_volume(self, vol):
        self.vol.set_property("volume", vol)

    def get_volume(self):
        self.vol.get_property("volume")


# vim: et sts=4 sw=4


"""The one reducer (fathers#90): the app state after an event, and the effects for the app to carry out. Keys and the
mouse go through the mode table (modes.py), which builds on state.py's transitions; everything else is answered by
those transitions here. Its own module, so neither state nor modes imports the other inside a function (fathers#141)."""
from __future__ import annotations

import dataclasses

from . import modes as _modes
from . import settings_model as _settings
from .state import (QUIT, AppState, DetailsLoaded, Drawn, Effect, Event, Key, Loaded, ModelFailed, ModelLoaded, Mouse,
                    NewsLoaded, Notice, Refresh, Result, SettingsChecked, SettingsFailed, SettingsOpened,
                    SettingsOverlay, SettingsRead, SettingsSections, SettingsWritten, ToolLoaded, closed_news,
                    PAGE_TABLES, model_loaded,
                    news_loaded, offsets, overlay_identity, scrolled, settings_context, settings_event, settings_result,
                    settle)
from .text import dashboard_text


def reduce(state: AppState, event: Event, now: float) -> Result:
    """The one reducer: the state after `event` at `now`, and what the app must do."""
    effects: list[Effect] = []
    if isinstance(event, (Key, Mouse)):  # only these scroll: a refresh that moves EVENTS down is no flash
        old, before, panels = state, offsets(state), offsets(state, False)  # Settings' offset before it moves
        state, effects = _modes.dispatch(state, event, now)
        state, marked = closed_news(old, state, QUIT in effects)
        effects = marked + effects  # saved before the app quits
        same = overlay_identity(state) == overlay_identity(old)  # an overlay opened or switched is not scrolled
        moved = scrolled(before if same else panels, offsets(state, same))
        if moved:
            state = dataclasses.replace(state, glow={**state.glow, **{name: now for name in moved}})
    elif isinstance(event, ModelLoaded):
        state = model_loaded(state, event)
    elif isinstance(event, ModelFailed):
        state = dataclasses.replace(state, refresh=Refresh("stale", dashboard_text(event.error)))
    elif isinstance(event, DetailsLoaded):
        state = dataclasses.replace(state, details={**state.details, event.key: Loaded(
            now, event.lane, event.cwd, None if event.error is None else dashboard_text(event.error))})
    elif isinstance(event, SettingsOpened):
        context = settings_context(state)
        requests = [_settings.Request("open")] if event.loading else []
        state, effects = settings_result(state, _settings.opened(event.sections, context, now, event.loading), context,
                                         requests)
    elif isinstance(event, (SettingsSections, SettingsRead, SettingsChecked, SettingsWritten)):
        state, effects = settings_event(state, event, now)
    elif isinstance(event, ToolLoaded):
        state = dataclasses.replace(state, tools={**state.tools, event.tool.name: event.tool})
    elif isinstance(event, NewsLoaded):
        state = news_loaded(state, event.news)
    elif isinstance(event, SettingsFailed):
        state = dataclasses.replace(state, notice=Notice(dashboard_text(event.error), now))
    elif isinstance(event, Drawn):
        home = PAGE_TABLES[state.page]  # QUEUE, or the page's table: where the focus goes back to
        focus = state.focus if state.focus == home or state.focus in event.frame.tables else home
        state = dataclasses.replace(state, frame=event.frame, focus=focus)
        if isinstance(state.overlay, SettingsOverlay):  # another size: the focused row kept in view
            state = dataclasses.replace(state, overlay=_settings.place(state.overlay, settings_context(state)))
    else:
        second = event.moment.replace(microsecond=0) if event.moment is not None else state.moment
        if second != state.moment:
            state = dataclasses.replace(state, moment=second)
        if isinstance(state.overlay, SettingsOverlay):  # a change that settled is saved, reads come due
            state, effects = settings_event(state, event, now)
    state, more = settle(state, now)
    return state, effects + more

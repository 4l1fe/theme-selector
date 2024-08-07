#!/home/kvdm/.pyenv/versions/selector/bin/python
from pathlib import Path
from argparse import ArgumentParser
from collections import UserString, defaultdict
from functools import partial
from enum import StrEnum, auto
from itertools import cycle

import tomlkit
import attrs
from cattrs.preconf.tomlkit import make_converter
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.layout.containers import Window, HSplit, VerticalAlign, ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings, ConditionalKeyBindings
from prompt_toolkit.widgets import Label
from prompt_toolkit.styles import Style
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.document import Document


MAROON_STYLE = 'bg:ansibrightgreen fg:black'
HIDDEN_STYLE = 'hidden'
DARK_THEME = 'dark'
LIGHT_THEME = 'light'
BUFFER_SEARCH = 'search-line'
BUFFER_COMMENT = 'comment-line'
toml_converter = make_converter()


class Mode:
    switch_flag = True
    
    @classmethod
    def is_search_mode(cls):
        return Mode.switch_flag

    @classmethod
    def is_comment_mode(cls):
        return not Mode.switch_flag
    
    @classmethod
    def switch_mode(cls):
        cls.switch_flag = not cls.switch_flag
    

is_search_mode_f = Condition(Mode.is_search_mode)
is_comment_mode_f = Condition(Mode.is_comment_mode)


def to_defaultdict(default_factory, data):
    obj = defaultdict(default_factory)

    for name, properties in data.items():
        obj[name] = default_factory(**properties)

    return obj


class ThemeModeEnum(StrEnum):
    unset = auto()
    light = auto()
    dark = auto()


@attrs.define
class LineStringProperties:
    """Object that expose every single line properties in the config.
    Setting new props is disallowed slot=True"""
    
    pinned: bool = False
    comment: str = ''
    theme_mode: ThemeModeEnum = attrs.field(default=ThemeModeEnum.unset,
                                            converter=lambda value: ThemeModeEnum(value))
    _cycled_theme: ThemeModeEnum = attrs.field(init=False)

    def __attrs_post_init__(self):
        '''Init cycled theme attr'''
        self._cycled_theme = cycle(ThemeModeEnum)

        while True:
            theme_mode = next(self._cycled_theme)
            if theme_mode == self.theme_mode:
                break

    def is_theme_set(self):
        return self.theme_mode != ThemeModeEnum.unset

    def set_next_theme(self) -> ThemeModeEnum:
        '''Promote cycled and update current state'''
        self.theme_mode = next(self._cycled_theme)
        return self.theme_mode


@attrs.define
class SelectorConfig:
    """Model to read and write line properties to. Automatically construct a hierarchical structure"""
    
    properties = attrs.field(default=to_defaultdict(LineStringProperties, {}),
                             converter=partial(to_defaultdict, LineStringProperties))
    
    @staticmethod
    def load(config_path: Path) -> 'SelectorConfig':
        if not config_path.exists():
            return toml_converter.structure({}, SelectorConfig)

        text = config_path.read_text()
        config = toml_converter.loads(text, SelectorConfig)

        return config

    def dump(self, config_path: Path):
        config_path.write_text(toml_converter.dumps(self))


class FormattedLineString(UserString):
    """The wrapper of the builtin string type. Mostly needed to display customization"""
    
    _theme_char = {ThemeModeEnum.dark: 'D',
                   ThemeModeEnum.light: 'L'}
    _pin_char = '*'

    def __init__(self, value: str,
                 properties: LineStringProperties = LineStringProperties()):
        self.value = value
        self.props = properties
        self.pinned = properties.pinned

        super().__init__(self._make_formatted_value())
    
    def toggle_pin(self) -> bool:
        # self.props.pinned = not self.props.pinned
        self.pinned = not self.pinned
        self._update_data()
        return self.pinned

    def is_pinned(self) -> bool:
        return self.pinned

    def get_comment(self):
        return self.props.comment
    
    def update_comment(self, text):
        self.props.comment = text
        self._update_data()
        
    def _make_formatted_value(self):
        data = self.value

        # if self.props.pinned:
        if self.pinned:
            data = self._pin_char + ' ' + data
            
        theme_char = self._theme_char[self.props.theme_mode] if self.props.is_theme_set() else ' '
        data = theme_char + ' ' + data

        if self.props.comment:
            data = data + '   # ' + self.props.comment

        data += '\n'

        return data

    def switch_theme(self) -> ThemeModeEnum:
        self.props.set_next_theme()
        self._update_data()
        return self.props.theme_mode

    def get_props(self) -> LineStringProperties:
        return self.props

    def _update_data(self):
        self.data = self._make_formatted_value()


@attrs.define
class FormattedLine:
    """The structure of FormattedTextControl.text.
    Example ('bg:white fg:black', 'arbitrary string value')"""
    
    style = attrs.field(type=str)
    string = attrs.field(type=FormattedLineString)


class LineStringSelector:
    """The main object to deal with the theme configs data interactively"""
    
    def __init__(self, theme_names: list[str], config: SelectorConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.formatted_lines = self._create_formatted_lines(theme_names, config.properties)
        self._selected_idx = 0
        self._sel_confirmed = False
        self._typed_text = ''
        
        self._create_container()

    def get_lines(self):
        if not self.found_lines:
            return self.found_lines

        copied_lines = self.found_lines.copy()
        copied_lines[self._selected_idx] = FormattedLine(style='[SetCursorPosition]',
                                                         string=self.selected_line.string)
        copied_lines = sorted(copied_lines, key=lambda fl: fl.string.is_pinned(), reverse=True)
        # MUST BE converted to the tuple FormattedTextControl.text
        copied_lines = [attrs.astuple(line) for line in copied_lines]
        
        return copied_lines

    def get_selected_value(self):
        if self._sel_confirmed:
            return self.selected_line.string.value

    def switch_focus(self):
        Mode.switch_mode()

        buffc = self.search_buffer_control \
                if Mode.is_search_mode() \
                else self.comment_buffer_control   
        
        get_app().layout.focus(buffc)
        
    def hidden_mode_style(self):
        if Mode.is_comment_mode():
            return HIDDEN_STYLE
        
        return '' 
        
    def find_lines(self, buffer: Buffer):
        self._typed_text = buffer.document.text
        self._selected_idx = 0

    def sync_props(self, props_idx,  **props):
        """Only selected line is being updated"""
        for prop_name, prop_value in props.items():
            setattr(self.config.properties[props_idx], prop_name, prop_value)

        self.config.dump(self.config_path)

    def has_selected_line(self):
        return bool(self.selected_line)
    
    @property
    def lines_count(self) -> int:
        return len(self.found_lines)

    @property
    def found_lines(self) -> list[FormattedLine]:
        lines = self.formatted_lines
        
        if self._typed_text:
            lines = [fl for fl in lines
                     if self._typed_text in fl.string]

        sorted_lines = sorted(lines, key=lambda fl: fl.string.value.lower())
        sorted_lines = sorted(sorted_lines, key=lambda fl: fl.string.is_pinned(), reverse=True)

        return sorted_lines
    
    @property
    def selected_line(self) -> FormattedLine | None:
        if self.found_lines:
            return self.found_lines[self._selected_idx]
    
    @staticmethod            
    def _create_formatted_lines(theme_names, theme_props: dict[LineStringProperties]) -> list[FormattedLine]:
        formatted_lines = []
        for theme_name in theme_names:
            fl_string = FormattedLineString(theme_name)

            if theme_name in theme_props:
                props: LineStringProperties = theme_props[theme_name]
                fl_string = FormattedLineString(theme_name,
                                                properties=props)
                                                # pinned=props.pinned,
                                                # comment=props.comment)

            formatted_lines.append(FormattedLine(style='', string=fl_string))

        return formatted_lines

    def _create_container(self):
        has_selected_line = Condition(lambda: bool(self.selected_line))
        kb_select = KeyBindings()

        @kb_select.add('up')
        @kb_select.add('c-k')
        def up(event):
            self._selected_idx = max(0, self._selected_idx - 1)

        @kb_select.add('down')
        @kb_select.add('c-j')
        def down(event):
            self._selected_idx = min(self.lines_count - 1, self._selected_idx + 1)

        @kb_select.add("pageup")
        @kb_select.add("c-u")
        def pageup(event):
            if self.select_window.render_info:
                self._selected_idx = max(
                    0, self._selected_idx - len(self.select_window.render_info.displayed_lines)
                )

        @kb_select.add("pagedown")
        @kb_select.add("c-d")
        def pagedown(event):
            if self.select_window.render_info:
                self._selected_idx = min(
                    self.lines_count - 1,
                    self._selected_idx + len(self.select_window.render_info.displayed_lines),
                )

        @kb_select.add('enter', filter=has_selected_line)
        def confirm_selection(event):
            self._sel_confirmed = True
            event.app.exit()

        @kb_select.add('c-p', filter=has_selected_line)
        def pin_unpin(event):
            props_idx = self.selected_line.string.value  # MUST BE set before toggling, selected line is changed
            pinned = self.selected_line.string.toggle_pin()
            self.sync_props(props_idx, pinned=pinned)

        @kb_select.add('c-l', filter=has_selected_line)
        def switch_comment(event):
            self.comment_buffer_control.buffer.set_document(Document(self.selected_line.string.get_comment()))
            self.switch_focus()
            
        @kb_select.add('c-t', filter=has_selected_line)
        def set_theme_mode(event):
            # MUST BE set before toggling
            theme_mode = self.selected_line.string.switch_theme()
            self.sync_props(self.selected_line.string.value, theme_mode=theme_mode)
            
        kb_comment = KeyBindings()

        @kb_comment.add('c-l')
        def switch_search(event):
            self.switch_focus()
            
        @kb_comment.add('enter', filter=has_selected_line)
        def update_comment(event):
            new_comment = event.current_buffer.document.text
            self.selected_line.string.update_comment(new_comment)
            self.sync_props(self.selected_line.string.value, comment=new_comment)
            self.switch_focus()
        
        self.search_buffer_control = BufferControl(
                                        buffer=Buffer(name=BUFFER_SEARCH,
                                                      multiline=False,
                                                      on_text_changed=self.find_lines),
                                        input_processors=[
                                            BeforeInput([
                                                (MAROON_STYLE, 'Search:'),
                                            ]),
                                        ],
                                        key_bindings=kb_select)
        
        self.comment_buffer_control = BufferControl(
                                        buffer=Buffer(name=BUFFER_COMMENT,
                                                      multiline=False),
                                        input_processors=[
                                            BeforeInput([
                                                (MAROON_STYLE, 'Comment:'),
                                            ]),
                                        ],
                                        key_bindings=kb_comment)
        
        self.select_window = Window(content=FormattedTextControl(
                                                text=self.get_lines,
                                                show_cursor=False),
                                    cursorline=True,
                                    style=self.hidden_mode_style)
        
        self.container = HSplit([ConditionalContainer(
                                    Window(content=self.search_buffer_control,
                                           height=1),
                                    filter=is_search_mode_f),
                                 ConditionalContainer(
                                    Window(content=self.comment_buffer_control,
                                           height=1),
                                    filter=is_comment_mode_f),
                                 Window(char=' ', height=1),
                                 self.select_window])

    def __pt_container__(self):
        return self.container


style_table = Style([
    ('label', 'bg:ansiwhite fg:black'),
    ('cursor-line', MAROON_STYLE + ' nounderline nohidden'),
])


def select(alacritty_themes_path, selector_config_path):
    """Run an IO loop, select a line, get a value from one"""
        
    theme_names = [path.name for path in alacritty_themes_path.iterdir()]

    config = SelectorConfig.load(selector_config_path)
    selector = LineStringSelector(theme_names, config, selector_config_path)
    
    kb_app = KeyBindings()

    @kb_app.add('c-q')
    @kb_app.add('c-c')
    def exit_(event):
        event.app.exit()
        
    layout = Layout(HSplit([
                        selector,
                        # Search help
                        ConditionalContainer(
                            Label([
                            (MAROON_STYLE, 'Search:'), ('', ' Type text to search '),
                            (MAROON_STYLE, 'Navigate:'), ('', ' up, down, pgup, pgdw, Ctrl+j/k, Ctrl+d/u '),
                            (MAROON_STYLE, 'Pin:'), ('', ' Ctrl+p '),
                            (MAROON_STYLE, 'Quit:'), ('', ' Ctrl+q/c '),],
                            style='class:label',
                            wrap_lines=False),
                            filter=is_search_mode_f,
                        ),
                        # Comment help
                        ConditionalContainer(
                            Label([
                            (MAROON_STYLE, 'Comment:'), ('', ' Write a comment to save '),
                            (MAROON_STYLE, 'Save:'), ('', ' Enter '),
                            (MAROON_STYLE, 'Quit:'), ('', ' Ctrl+q/c '),],
                            style='class:label',
                            wrap_lines=False),
                            filter=is_comment_mode_f),
                        ],
                        align=VerticalAlign.JUSTIFY),
                    focused_element=selector)

    app = Application(
        layout=layout,
        key_bindings=kb_app,
        full_screen=True,
        style=style_table
    )
    app.run()

    selected_config_name = selector.get_selected_value()
    return selected_config_name


def write(selected_config_name, alacritty_themes_path, alacritty_config_path, posh_config_path):
    """Update two toml configs. The Posh theme light, dark mode should correspond the alacritty theme mode"""
    
    selected_theme_path = alacritty_themes_path / selected_config_name

    with open(alacritty_config_path, 'r') as file_a, open(posh_config_path, 'r') as file_p:
        document_a = tomlkit.load(file_a)
        document_p = tomlkit.load(file_p)

    # 'import' 0 - theme file path
    document_a['import'][0] = selected_theme_path.as_posix()
    # change OhMyPosh theme correspondingly
    document_p['palettes']['template'] = 'latte' if 'light' in selected_config_name else 'frappe'

    with open(alacritty_config_path, 'w') as file_a, open(posh_config_path, 'w') as file_p:
        tomlkit.dump(document_a, file_a)
        tomlkit.dump(document_p, file_p)


def change(alacritty_themes_path, alacritty_config_path, posh_config_path, selector_config_path):
    selected_path = select(alacritty_themes_path, selector_config_path)
    if selected_path:
        write(selected_path, alacritty_themes_path, alacritty_config_path, posh_config_path)


def expanded_path_type(string) -> Path:
    return Path(string).expanduser()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--alacritty-themes-path', required=True, type=expanded_path_type)
    parser.add_argument('--alacritty-config-path', required=True, type=expanded_path_type)
    parser.add_argument('--posh-config-path', required=True, type=expanded_path_type)
    parser.add_argument('--selector-config-path', type=expanded_path_type, default='~/.config/selector-config.toml')
    args = parser.parse_args()

    change(args.alacritty_themes_path, args.alacritty_config_path, args.posh_config_path, args.selector_config_path)

#!/home/vol/.pyenv/versions/selector/bin/python
from pathlib import Path
from collections import namedtuple
from argparse import ArgumentParser
from collections import UserString, defaultdict
from functools import partial

import tomlkit
import attrs
from cattrs.preconf.tomlkit import make_converter
from prompt_toolkit.application import Application
from prompt_toolkit.layout.containers import Window, HSplit, VerticalAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import Label
from prompt_toolkit.styles import Style
from prompt_toolkit.buffer import Buffer


FormattedLine = namedtuple('FormattedLine', ['style', 'string'])
MAROON_STYLE = 'bg:ansibrightgreen fg:black'
DARK_THEME = 'dark'
LIGHT_THEME = 'light'
toml_converter = make_converter()


def to_defaultdict(default_factory, data):
    obj = defaultdict(default_factory)
    
    for name, properties in data.items():
        obj[name] = default_factory(**properties)
    
    return obj


@attrs.define
class LineStringProperties:
    pinned: bool = False
    comment: str = ''
    theme_group: str = LIGHT_THEME
    
    
@attrs.define
class SelectorConfig:
    properties = attrs.field(converter=partial(to_defaultdict, LineStringProperties),
                             default=to_defaultdict(LineStringProperties, {}))

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

    def __init__(self, value: str, pinned: bool = False, comment: str = ''):
        self.value = value
        self._init_value = value
        self._pinned = pinned
        self._pin_char = '*'
        self._comment = comment
        
        init_data = self._make_data()
        super().__init__(init_data)
    
    def toggle_pin(self) -> bool:
        self._pinned = not self._pinned
        self._update_data()
        
        return self._pinned
    
    def is_pinned(self) -> bool:
        return self._pinned
    
    def _make_data(self):
        data = self.value
        
        if self._pinned:
            data = self._pin_char + ' ' + data
            
        if self._comment:
            data = data + '   # ' + self._comment
            
        data += '\n'
        
        return data
    
    def _update_data(self):
        self.data = self._make_data()
        
        
class LineSelector:
    
    def __init__(self, values: list[str], config: SelectorConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.formatted_values = self._create_formatted_lines(values, config.properties)
        self._selected_idx = 0
        self._selected_value = None
        self._fuzzy_text = ''
        self._create_container()
        
    def get_text_lines(self):
        if not self.found_values:
            return self.found_values
            
        selected_line = self.found_values[self._selected_idx]
        
        copied_values = self.found_values.copy()
        copied_values[self._selected_idx] = FormattedLine(style='[SetCursorPosition]',
                                                          string=selected_line.string)
        copied_values = sorted(copied_values, key=lambda fl: fl.string.is_pinned(), reverse=True)
        
        return copied_values
    
    def get_selected_value(self):
        return self._selected_value
    
    def fuzzy_find(self, buffer: Buffer):
        self._fuzzy_text = buffer.document.text
        self._selected_idx = 0
        
    @property
    def values_len(self) -> int:
        return len(self.found_values)
    
    @property
    def found_values(self) -> list[FormattedLine]:
        values = self.formatted_values
        if len(self._fuzzy_text) > 0:
            values = [fl for fl in values 
                    if self._fuzzy_text in fl.string]
        
        sorted_values = sorted(values, key=lambda fl: fl.string.value.lower())
        sorted_values = sorted(sorted_values, key=lambda fl: fl.string.is_pinned(), reverse=True)
        
        return sorted_values

    def _create_formatted_lines(self, values, property_lines: dict[LineStringProperties]) -> list[FormattedLine]:
        formatted_lines = []
        for value in values:
            fls = FormattedLineString(value)
            
            if value in property_lines:
                lproperties: LineStringProperties = property_lines[value]
                fls = FormattedLineString(value, 
                                          pinned=lproperties.pinned,
                                          comment=lproperties.comment)
            
            formatted_lines.append(FormattedLine(style='', string=fls))
        
        return formatted_lines
            
    def _create_container(self):
        kb_select = KeyBindings()
        has_values = Condition(lambda: bool(self.found_values)
                               )
        @kb_select.add('up')
        @kb_select.add('c-k')
        def kb_up(event):
            self._selected_idx = max(0, self._selected_idx - 1)

        @kb_select.add('down')
        @kb_select.add('c-j')
        def kb_down(event):
            self._selected_idx = min(self.values_len - 1, self._selected_idx + 1)
        
        @kb_select.add("pageup")
        @kb_select.add("c-u")
        def _pageup(event):
            if self.select_window.render_info:
                self._selected_idx = max(
                    0, self._selected_idx - len(self.select_window.render_info.displayed_lines)
                )

        @kb_select.add("pagedown")
        @kb_select.add("c-d")
        def _pagedown(event):
            if self.select_window.render_info:
                self._selected_idx = min(
                    self.values_len - 1,
                    self._selected_idx + len(self.select_window.render_info.displayed_lines),
                )

        @kb_select.add('enter')
        def kb_enter(event):
            if self.found_values:
                selected_line = self.found_values[self._selected_idx]
                self._selected_value = selected_line.string.value
                event.app.exit()
                
        @kb_select.add('c-p')
        def pin_unpin(event):
            if self.found_values:
                selected_line = self.found_values[self._selected_idx]
                pinned = selected_line.string.toggle_pin()
                
                self.config.properties[selected_line.string.value].pinned = pinned                
                self.config.dump(self.config_path)
        
        type_buffer = Buffer(name='fuzzyline',
                             on_text_changed=self.fuzzy_find)
        type_window = Window(content=BufferControl(
                                        buffer=type_buffer,
                                        input_processors=[
                                            BeforeInput([
                                                (MAROON_STYLE, 'Search:'),
                                            ]), 
                                        ],
                                        key_bindings=kb_select),
                             height=1)   
        self.select_window = Window(content=FormattedTextControl(
                                                text=self.get_text_lines,
                                                show_cursor=False,),
                                    cursorline=True)   
        self.container = HSplit([type_window,
                                 Window(char=' ', height=1),
                                 self.select_window])           

    def __pt_container__(self):
        return self.container
        
        
style = Style([
    ('label', 'bg:ansiwhite fg:black'),
    ('cursor-line', MAROON_STYLE + ' nounderline'),
])


def select(alacritty_themes_path, selector_config_path):
    kb_app = KeyBindings()
    
    @kb_app.add('c-q')
    @kb_app.add('c-c')
    def exit_(event):
        event.app.exit()
        
    theme_paths = list(alacritty_themes_path.iterdir())
    theme_names = [path.name for path in theme_paths]
    
    config = SelectorConfig.load(selector_config_path)
    selector = LineSelector(theme_names, config, selector_config_path)

    window = HSplit([
                    selector,
                    Label([(MAROON_STYLE, 'Quit:'), ('', ' Press `Ctrl+q/c` '),
                           (MAROON_STYLE, 'Type:'), ('', ' Type text to search '),
                           (MAROON_STYLE, 'Navigate:'), ('', ' Press `up, down, pgup, pgdw, Ctrl+j/k, Ctrl+d/u` '),
                           (MAROON_STYLE, 'Pin:'), ('', ' Press `Ctrl+p` '),],
                        style='class:label',
                        wrap_lines=False)
                    ],
                    align=VerticalAlign.JUSTIFY)

    layout = Layout(window, focused_element=selector)

    app = Application(
        layout=layout,
        key_bindings=kb_app,
        full_screen=True,
        style=style
    )
    app.run()
    
    selected_config_name = selector.get_selected_value()
    return selected_config_name
    

def write(selected_config_name, alacritty_themes_path, alacritty_config_path, posh_config_path):
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
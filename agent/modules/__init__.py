from . import (
    actions,
    clipboard,
    files,
    inputctl,
    media,
    notify,
    power,
    processes,
    ram,
    screen,
    stream,
    system,
    terminal,
)

# notify first: other modules push into its notification center
MODULES = [notify, system, power, clipboard, files, media, inputctl, screen, processes, ram, stream,
           actions, terminal]

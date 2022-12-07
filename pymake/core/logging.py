import logging
from termcolor import colored

def merge(lhs, rhs):
    if type(lhs) != type(rhs):
        raise RuntimeError(f'cannot merge {type(lhs)} with {rhs}')
    if type(lhs) == dict:
        for k in rhs:
            if k in lhs:
                lhs[k] = merge(lhs[k], rhs[k])
            else:
                lhs[k] = rhs[k]
    elif type(lhs) == list:
        lhs.extend(rhs)
    return lhs


class bind_back:
    def __init__(self, fn, *args):
        self.fn = fn
        self.args = args
    
    def __call__(self, *args, **kwds):
        return self.fn(*args, *self.args, **kwds)

class ColoredFormatter(logging.Formatter):

    COLORS = {
        'WARNING': bind_back(colored, 'yellow'),
        'INFO': bind_back(colored, 'green'),
        'DEBUG': bind_back(colored, 'cyan'),
        'CRITICAL': bind_back(colored, 'yellow'),
        'ERROR': bind_back(colored, 'red')
    }

    COLORS_ATTRS = {
        'WARNING': list(),
        'INFO': list(),
        'DEBUG': list(),
        'CRITICAL': ['blink'],
        'ERROR': ['blink']
    }

    COLOR_FORMAT = \
        f"[{colored('%(asctime)s.%(msecs)03d', 'grey')}]" \
        f"[%(levelname)s][{colored('%(name)s', 'white', attrs=['bold'])}]: %(message)s "\
        f"({colored('%(filename)s:%(lineno)d', 'grey')})"
    
    FORMAT = \
        "[%(asctime)s.%(msecs)03d]" \
        "[%(levelname)s][%(name)s]: %(message)s "\
        "(%(filename)s:%(lineno)d)"

    def __init__(self, use_color=True):
        super().__init__(self.COLOR_FORMAT if use_color else self.FORMAT, datefmt='%H:%M:%S')
        self.use_color = use_color

    def format(self, record):
        levelname = record.levelname
        if self.use_color and levelname in self.COLORS:
            record.levelname = self.COLORS[levelname](levelname, attrs=['bold', *self.COLORS_ATTRS[levelname]])
            record.msg = self.COLORS[levelname](record.msg, attrs=[])
        return super().format(record)


class ColoredLogger(logging.Logger):

    def __init__(self, name):
        super().__init__(name)
        self.propagate = False

        color_formatter = ColoredFormatter()
        if not self.hasHandlers():
            console = logging.StreamHandler()
            console.setFormatter(color_formatter)
            self.addHandler(console)
        else:
            for handler in self.handlers:
                handler.setFormatter(color_formatter)


logging.setLoggerClass(ColoredLogger)

class Logging:
    def __init__(self, name: str = None) -> None:
        if name is None:
            name = self.__class__.__name__
        self._logger = logging.getLogger(name)
        self.debug = self._logger.debug
        self.info = self._logger.info
        self.warn = self._logger.warn
        self.error = self._logger.error

"""One background asyncio loop, one printer session, no print queue or replay."""
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import threading

from .printer_settings import printer_settings, save_printer


@dataclass(frozen=True)
class PrinterStatus:
    state: str = 'stopped'
    address: str = ''
    name: str = ''
    detail: str = ''
    busy: bool = False
    calibration: bool = False


class ThermalPrinterService:
    def __init__(self, settings, config_path, *, factory=None, saver=save_printer):
        self.settings = printer_settings(settings)
        self.config_path = Path(config_path).resolve()
        self._factory = factory
        self._saver = saver
        self._guard = threading.Lock()
        self._status = PrinterStatus(address=self.settings['address'], name=self.settings['name'])
        self._thread = None
        self._loop = None
        self._started = threading.Event()
        self._printer = self._backend = None
        self._wanted = self.settings['auto_connect']
        self._closing = False
        self._next_attempt = 0.0
        self._active = None
        self._monitor = None

    @property
    def status(self):
        with self._guard:
            return self._status

    def _update(self, **kwargs):
        with self._guard:
            self._status = replace(self._status, **kwargs)

    def _create(self):
        if self._factory is not None:
            return self._factory()
        library = self.settings['library_path']
        if library and not getattr(sys, 'frozen', False):
            source = (self.config_path.parent / library / 'src').resolve()
            if source.is_dir():
                sys.path.insert(0, str(source))
        import cyberwall_id_printer as api
        if not hasattr(api.Printer, 'check_connection'):
            raise RuntimeError('Обнови cyberwall_id_printer до версии 0.3.0 или новее.')
        paper = dict(self.settings['paper'])
        paper['paper_type'] = paper.pop('type')
        path = '' if getattr(sys, 'frozen', False) else self.settings['timini_path']
        backend = api.TiMiniBackend(
            (self.config_path.parent / path).resolve() if path else None,
            blackening=self.settings['blackening'], timeout=self.settings['scan_timeout_seconds'])
        return backend, api.Printer(backend, media=api.MediaConfig(**paper))

    def start(self):
        if self._thread is not None:
            return
        if not self.settings['enabled']:
            self._update(state='disabled', detail='Отключён в config.toml.')
            return
        self._thread = threading.Thread(target=self._run, name='thermal-printer', daemon=True)
        self._thread.start()
        self._started.wait()

    def _run(self):
        with asyncio.Runner() as runner:
            self._loop = runner.get_loop()
            try:
                self._backend, self._printer = self._create()
                self._operation_lock = asyncio.Lock()
                self._update(state='disconnected' if self.status.address else 'unselected')
                self._monitor = self._loop.create_task(self._watch())
            except Exception as error:
                self._update(state='unavailable', detail=f'Модуль термопринтера недоступен: {error}')
                self._started.set()
                return
            self._loop.call_soon(self._started.set)
            self._loop.run_forever()

    async def _disconnect(self):
        try:
            await self._printer.disconnect()
        except Exception:
            # Cleanup must not hide the original transmission/connection error.
            pass
        self._update(calibration=False)

    async def _connect(self, address, *, remember=False, name=''):
        self._update(state='connecting', detail='Подключение к термопринтеру…')
        await self._disconnect()
        try:
            caps = await self._printer.connect(address)
        except BaseException:
            await self._disconnect()
            raise
        self._update(state='connected', address=caps.address, name=caps.name or name,
                     detail='', calibration=caps.calibration)
        if remember:
            self.settings['address'], self.settings['name'] = caps.address, caps.name or name
            try:
                self._saver(self.config_path, caps.address, caps.name or name)
            except Exception as error:
                self._update(detail=f'Подключено, но выбор не сохранён в config.toml: {error}')

    def _retry_later(self):
        self._next_attempt = self._loop.time() + self.settings['reconnect_delay_seconds']
        if not self.settings['auto_reconnect']:
            self._wanted = False

    async def _watch(self):
        while True:
            try:
                if not self.status.busy and not self._closing:
                    async with self._operation_lock:
                        if self.status.busy:
                            continue
                        if self.status.state == 'connected':
                            if not await self._printer.check_connection():
                                await self._disconnect()
                                self._update(state='disconnected', detail='Связь с термопринтером потеряна.')
                                self._retry_later()
                        elif (self._wanted and self.settings['address']
                              and self._loop.time() >= self._next_attempt):
                            try:
                                await self._connect(self.settings['address'])
                            except Exception as error:
                                self._update(state='disconnected', detail=str(error))
                                self._retry_later()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._disconnect()
                self._update(state='disconnected', detail=str(error))
                self._retry_later()
            await asyncio.sleep(self.settings['connection_check_seconds'])

    def _submit(self, action, *, require_connected=False):
        with self._guard:
            if self._closing or self._loop is None or not self._loop.is_running():
                raise RuntimeError(self._status.detail or 'Сервис термопринтера не запущен.')
            if self._status.busy:
                raise RuntimeError('Термопринтер уже выполняет операцию. Новое задание не добавлено.')
            if require_connected and self._status.state != 'connected':
                raise RuntimeError('Термопринтер не подключён. Задание не сохранено; повтори после подключения.')
            self._status = replace(self._status, busy=True)
        return asyncio.run_coroutine_threadsafe(self._execute(action), self._loop)

    async def _execute(self, action):
        self._active = asyncio.current_task()
        try:
            async with self._operation_lock:
                return await action()
        finally:
            self._active = None
            self._update(busy=False)

    def scan(self):
        async def action():
            return await self._backend.scan()
        return self._submit(action)

    def select(self, address, name=''):
        if not isinstance(address, str) or not address.strip():
            raise ValueError('Укажи адрес термопринтера.')
        async def action():
            self._wanted = True
            try:
                await self._connect(address.strip(), remember=True, name=name)
            except Exception as error:
                self._update(state='disconnected', detail=str(error))
                self._retry_later()
                raise
        return self._submit(action)

    def reconnect(self):
        if not self.settings['address']:
            raise ValueError('Сначала выбери термопринтер.')
        return self.select(self.settings['address'], self.settings['name'])

    def disconnect(self):
        async def action():
            self._wanted = False
            await self._disconnect()
            self._update(state='disconnected', detail='Отключён вручную. Автоподключение приостановлено.')
        return self._submit(action)

    def print_label(self, account_id=None, name=None, *, mode='id-name', copies=1):
        if type(copies) is not int or not 1 <= copies <= self.settings['max_copies']:
            raise ValueError(f"Количество: целое число 1…{self.settings['max_copies']}.")
        async def action():
            # A job submitted before a disconnect must never run after reconnect.
            if self.status.state != 'connected' or not await self._printer.check_connection():
                await self._disconnect()
                self._update(state='disconnected', detail='Связь потеряна. Задание не отправлено.')
                self._retry_later()
                raise RuntimeError('Связь потеряна. Повтори печать вручную после подключения.')
            try:
                return await self._printer.print_id(account_id, name, mode=mode, copies=copies,
                    inactive=True if mode.startswith('blank-') else self.settings['layout']['inactive_segments'],
                    decorations=self.settings['layout']['decorations'])
            except Exception as error:
                if getattr(error, 'may_have_printed', False):
                    await self._disconnect()
                    self._update(state='disconnected', detail='Передача прервана. Задание удалено, повтора не будет.')
                    self._retry_later()
                    raise RuntimeError('Передача прервана. Часть серии могла напечататься. Автоматического повтора не будет.') from error
                raise
        return self._submit(action, require_connected=True)

    def calibrate(self):
        async def action():
            try:
                await self._printer.calibrate()
            except Exception:
                # A calibration failure can leave library session uncertain.
                if not await self._printer.check_connection():
                    await self._disconnect()
                    self._update(state='disconnected', detail='Калибровка прервана.')
                    self._retry_later()
                raise
        return self._submit(action, require_connected=True)

    async def _shutdown(self):
        self._monitor.cancel()
        await asyncio.gather(self._monitor, return_exceptions=True)
        if self._active is not None:
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
        await self._disconnect()
        self._update(state='stopped', busy=False)

    def close(self):
        self._closing = True
        if self._thread is None or not self._thread.is_alive():
            return
        if self._monitor is None:
            self._thread.join()
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()

"""Support for functionality to download files."""
from http import HTTPStatus
import logging
import os
import re
import threading

import requests
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import raise_if_invalid_filename, raise_if_invalid_path

_LOGGER = logging.getLogger(__name__)

ATTR_FILENAME = "filename"
ATTR_SUBDIR = "subdir"
ATTR_URL = "url"
ATTR_OVERWRITE = "overwrite"

CONF_DOWNLOAD_DIR = "download_dir"

DOMAIN = "downloader"
DOWNLOAD_FAILED_EVENT = "download_failed"
DOWNLOAD_COMPLETED_EVENT = "download_completed"

SERVICE_DOWNLOAD_FILE = "download_file"

SERVICE_DOWNLOAD_FILE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_URL): cv.url,
        vol.Optional(ATTR_SUBDIR): cv.string,
        vol.Optional(ATTR_FILENAME): cv.string,
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({vol.Required(CONF_DOWNLOAD_DIR): cv.string})},
    extra=vol.ALLOW_EXTRA,
)


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Listen for download events to download files."""
    download_path = config[DOMAIN][CONF_DOWNLOAD_DIR]

    # If path is relative, we assume relative to Home Assistant config dir
    if not os.path.isabs(download_path):
        download_path = hass.config.path(download_path)

    if not os.path.isdir(download_path):
        _LOGGER.error(
            "Download path %s does not exist. File Downloader not active", download_path
        )

        return False

    def download_file(service: ServiceCall) -> None:
        """Start thread to download file specified in the URL."""

    class FileDownloader:
        def __init__(self, hass, download_path, service):
        self.hass = hass
        self.download_path = download_path
        self.service = service
        self.final_path = None
        self.filename = None
        self.url = service.data[ATTR_URL]

    def _get_filename_from_headers(self, headers):
        if "content-disposition" in headers:
            match = re.findall(r"filename=(\S+)", headers["content-disposition"])
            if match:
                return match[0].strip("'\" ")
        return None

    def _prepare_file_path(self):
        subdir = self.service.data.get(ATTR_SUBDIR)
        self.filename = self.service.data.get(ATTR_FILENAME)
        overwrite = self.service.data.get(ATTR_OVERWRITE)

        if subdir:
            raise_if_invalid_path(subdir)

        if self.filename is None:
            self.filename = os.path.basename(self.url).strip()

        if not self.filename:
            self.filename = "ha_download"

        raise_if_invalid_filename(self.filename)

        if subdir:
            subdir_path = os.path.join(self.download_path, subdir)
            os.makedirs(subdir_path, exist_ok=True)
            self.final_path = os.path.join(subdir_path, self.filename)
        else:
            self.final_path = os.path.join(self.download_path, self.filename)

        if not overwrite:
            path, ext = os.path.splitext(self.final_path)
            tries = 1
            while os.path.isfile(self.final_path):
                tries += 1
                self.final_path = f"{path}_{tries}.{ext}"

    def _download_file(self, req):
        with open(self.final_path, "wb") as fil:
            for chunk in req.iter_content(1024):
                fil.write(chunk)

    def _fire_event(self, event_name):
        self.hass.bus.fire(
            f"{DOMAIN}_{event_name}",
            {"url": self.url, "filename": self.filename},
        )

    def download(self):
        try:
            req = requests.get(self.url, stream=True, timeout=10)

            if req.status_code != HTTPStatus.OK:
                _LOGGER.warning("Downloading '%s' failed, status_code=%d", self.url, req.status_code)
                self._fire_event(DOWNLOAD_FAILED_EVENT)
                return

            if self.filename is None:
                self.filename = self._get_filename_from_headers(req.headers)

            self._prepare_file_path()
            self._download_file(req)
            _LOGGER.debug("Downloading of %s done", self.url)
            self._fire_event(DOWNLOAD_COMPLETED_EVENT)

        except requests.exceptions.RequestException as ex:
            _LOGGER.exception("Request error occurred: %s", str(ex))
            self._fire_event(DOWNLOAD_FAILED_EVENT)
            if self.final_path and os.path.isfile(self.final_path):
                os.remove(self.final_path)
        except ValueError:
            _LOGGER.exception("Invalid value")
            self._fire_event(DOWNLOAD_FAILED_EVENT)
            if self.final_path and os.path.isfile(self.final_path):
                os.remove(self.final_path)

# In the setup function:
def download_file(service: ServiceCall) -> None:
    downloader = FileDownloader(hass, download_path, service)
    threading.Thread(target=downloader.download).start()

    threading.Thread(target=do_download).start()

    hass.services.register(
        DOMAIN,
        SERVICE_DOWNLOAD_FILE,
        download_file,
        schema=SERVICE_DOWNLOAD_FILE_SCHEMA,
    )

    return True

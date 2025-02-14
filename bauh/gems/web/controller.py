import glob
import json
import locale
import os
import re
import shutil
import subprocess
import traceback
from math import floor
from pathlib import Path
from threading import Thread
from typing import List, Type, Set, Tuple, Optional, Dict, Generator, Iterable

import requests
import yaml
from colorama import Fore
from requests import Response

from bauh.api.abstract.context import ApplicationContext
from bauh.api.abstract.controller import SoftwareManager, SearchResult, UpgradeRequirements, TransactionResult, \
    SoftwareAction
from bauh.api.abstract.disk import DiskCacheLoader
from bauh.api.abstract.handler import ProcessWatcher, TaskManager
from bauh.api.abstract.model import SoftwarePackage, CustomSoftwareAction, PackageSuggestion, PackageUpdate, \
    PackageHistory, \
    SuggestionPriority, PackageStatus
from bauh.api.abstract.view import MessageType, MultipleSelectComponent, InputOption, SingleSelectComponent, \
    SelectViewType, TextInputComponent, FormComponent, FileChooserComponent, ViewComponent, PanelComponent
from bauh.api.paths import DESKTOP_ENTRIES_DIR
from bauh.commons import resource
from bauh.commons.boot import CreateConfigFile
from bauh.commons.html import bold
from bauh.commons.system import ProcessHandler, get_dir_size, SimpleProcess
from bauh.commons.view_utils import get_human_size_str
from bauh.gems.web import INSTALLED_PATH, nativefier, DESKTOP_ENTRY_PATH_PATTERN, URL_FIX_PATTERN, ENV_PATH, UA_CHROME, \
    SUGGESTIONS_CACHE_FILE, ROOT_DIR, TEMP_PATH, FIX_FILE_PATH, ELECTRON_CACHE_DIR, \
    get_icon_path, URL_PROPS_PATTERN
from bauh.gems.web.config import WebConfigManager
from bauh.gems.web.environment import EnvironmentUpdater, EnvironmentComponent
from bauh.gems.web.model import WebApplication
from bauh.gems.web.search import SearchIndexManager
from bauh.gems.web.worker import SuggestionsManager, UpdateEnvironmentSettings, \
    SuggestionsLoader, SearchIndexGenerator

try:
    from bs4 import BeautifulSoup, SoupStrainer
    BS4_AVAILABLE = True
except:
    BS4_AVAILABLE = False


try:
    import lxml
    LXML_AVAILABLE = True
except:
    LXML_AVAILABLE = False

RE_PROTOCOL_STRIP = re.compile(r'[a-zA-Z]+://')
RE_SEVERAL_SPACES = re.compile(r'\s+')
RE_SYMBOLS_SPLIT = re.compile(r'[\-|_\s:.]')


class WebApplicationManager(SoftwareManager):

    def __init__(self, context: ApplicationContext, suggestions_loader: Optional[SuggestionsLoader] = None):
        super(WebApplicationManager, self).__init__(context=context)
        self.http_client = context.http_client
        self.env_updater = EnvironmentUpdater(logger=context.logger, http_client=context.http_client,
                                              file_downloader=context.file_downloader, i18n=context.i18n)
        self.enabled = True
        self.i18n = context.i18n
        self.logger = context.logger
        self.env_thread = None
        self.suggestions_loader = suggestions_loader
        self.suggestions = {}
        self.configman = WebConfigManager()
        self.idxman = SearchIndexManager(logger=context.logger)
        self._custom_actions: Optional[Iterable[CustomSoftwareAction]] = None

    def _get_lang_header(self) -> str:
        try:
            system_locale = locale.getdefaultlocale()
            return system_locale[0] if system_locale else 'en_US'
        except:
            return 'en_US'

    def clean_environment(self, root_password: Optional[str], watcher: ProcessWatcher) -> bool:
        handler = ProcessHandler(watcher)

        success = True
        for path in (ENV_PATH, ELECTRON_CACHE_DIR):
            self.logger.info("Checking path '{}'".format(path))
            if os.path.exists(path):
                try:
                    self.logger.info('Removing path {}'.format(path))
                    res, output = handler.handle_simple(SimpleProcess(['rm', '-rf', path]))

                    if not res:
                        success = False
                except:
                    watcher.print(traceback.format_exc())
                    success = False

        if success:
            watcher.show_message(title=self.i18n['success'].capitalize(),
                                 body=self.i18n['web.custom_action.clean_env.success'],
                                 type_=MessageType.INFO)
        else:
            watcher.show_message(title=self.i18n['error'].capitalize(),
                                 body=self.i18n['web.custom_action.clean_env.failed'],
                                 type_=MessageType.ERROR)

        return success

    def _get_app_name(self, url_no_protocol: str, soup: "BeautifulSoup") -> str:
        name_tag = soup.head.find('meta', attrs={'name': 'application-name'})
        name = name_tag.get('content') if name_tag else None

        if not name:
            name_tag = soup.head.find('title')
            name = name_tag.text.strip() if name_tag else None

        if not name:
            name = url_no_protocol.split('.')[0].strip()

        if name:
            name_split = [token for token in RE_SYMBOLS_SPLIT.split(name) if token]

            if len(name_split) == 1:
                name = name_split[0].strip()
            else:
                name = url_no_protocol

        return name

    def _get_app_icon_url(self, url: str, soup: "BeautifulSoup") -> str:
        for rel in ('icon', 'ICON'):
            icon_tag = soup.head.find('link', attrs={"rel": rel})
            icon_url = icon_tag.get('href') if icon_tag else None

            if icon_url and not icon_url.startswith('http'):
                if icon_url.startswith('//'):
                    icon_url = 'https:{}'.format(icon_url)
                elif icon_url.startswith('/'):
                    icon_url = url + icon_url
                else:
                    icon_url = url + '/{}'.format(icon_url)

            if icon_url:
                return icon_url

        if not icon_url:
            icon_tag = soup.head.find('meta', attrs={"property": 'og:image'})
            icon_url = icon_tag.get('content') if icon_tag else None

            if icon_url:
                return icon_url

    def _get_app_description(self, url: str,  soup: "BeautifulSoup") -> str:
        description = None
        desc_tag = soup.head.find('meta', attrs={'name': 'description'})

        if desc_tag:
            description = desc_tag.get('content')

        if not description:
            desc_tag = soup.find('title')
            description = desc_tag.text if desc_tag else url

        if description:
            try:
                utf8_desc = description.encode('iso-8859-1').decode('utf-8')
                description = utf8_desc
            except:
                pass

        return description

    def _get_fix_for(self, url_domain: str, electron_branch: str) -> str:
        fix_url = URL_FIX_PATTERN.format(domain=url_domain,
                                         electron_branch=electron_branch)

        try:
            res = self.http_client.get(fix_url, session=False)
            if res:
                return res.text
        except Exception as e:
            self.logger.warning("Error when trying to retrieve a fix for {}: {}".format(fix_url, e.__class__.__name__))

    def _get_custom_properties(self, url_domain: str, electron_branch: str) -> Optional[Dict[str, object]]:
        props_url = URL_PROPS_PATTERN.format(domain=url_domain,
                                             electron_branch=electron_branch)

        try:
            res = self.http_client.get(props_url, session=False)
            if res:
                props = {}
                for line in res.text.split('\n'):
                    line_strip = line.strip()
                    if line_strip:
                        line_split = line_strip.split('=', 1)

                        if len(line_split) == 2:
                            key, val = line_split[0].strip(), line_split[1].strip()

                            if key:
                                props[key] = val

                return props

        except Exception as e:
            self.logger.warning(f"Error when trying to retrieve custom installation properties for {props_url}: {e.__class__.__name__}")

    def _map_electron_branch(self, version: str) -> str:
        return f"electron_{'_'.join(version.split('.')[0:-1])}_X"

    def _strip_url_protocol(self, url: str) -> str:
        return RE_PROTOCOL_STRIP.split(url)[1].strip().lower()

    def serialize_to_disk(self, pkg: SoftwarePackage, icon_bytes: Optional[bytes], only_icon: bool):
        super(WebApplicationManager, self).serialize_to_disk(pkg=pkg, icon_bytes=None, only_icon=False)

    def _request_url(self, url: str) -> Optional[Response]:
        headers = {'Accept-language': self._get_lang_header(), 'User-Agent': UA_CHROME}

        try:
            return self.http_client.get(url, headers=headers, ignore_ssl=True, single_call=True, session=False, allow_redirects=True)
        except Exception as e:
            self.logger.warning(f"Could not GET '{url}'. Exception: {e.__class__.__name__}")

    def _map_url(self, url: str) -> Tuple["BeautifulSoup", requests.Response]:
        url_res = self._request_url(url)
        if url_res:
            return BeautifulSoup(url_res.text, 'lxml', parse_only=SoupStrainer('head')), url_res

    def search(self, words: str, disk_loader: DiskCacheLoader, limit: int = -1, is_url: bool = False) -> SearchResult:
        web_config = {}
        thread_config = Thread(target=self._fill_config_async, args=(web_config,))
        thread_config.start()

        res = SearchResult([], [], 0)

        installed = self.read_installed(disk_loader=disk_loader, limit=limit).installed

        if is_url:
            url = words[0:-1] if words.endswith('/') else words

            url_no_protocol = self._strip_url_protocol(url)

            installed_matches = [app for app in installed if self._strip_url_protocol(app.url) == url_no_protocol]

            if installed_matches:
                res.installed.extend(installed_matches)
            else:
                soup_map = self._map_url(url)

                if soup_map:
                    soup, response = soup_map[0], soup_map[1]

                    final_url = response.url

                    if final_url.endswith('/'):
                        final_url = final_url[0:-1]

                    name = self._get_app_name(url_no_protocol, soup)
                    desc = self._get_app_description(final_url, soup)
                    icon_url = self._get_app_icon_url(final_url, soup)

                    app = WebApplication(url=final_url, source_url=url, name=name, description=desc, icon_url=icon_url)

                    thread_config.join()
                    env_settings = self.env_updater.read_settings(web_config=web_config, cache=True)

                    if env_settings.get('electron') and env_settings['electron'].get('version'):
                        app.version = env_settings['electron']['version']
                        app.latest_version = app.version

                    res.new = [app]
        else:
            lower_words = words.lower().strip()
            installed_matches = [app for app in installed if lower_words in app.name.lower()]

            index = self.idxman.read()

            if not index and self.suggestions_loader and self.suggestions_loader.is_alive():
                self.suggestions_loader.join()
                index = self.idxman.read()

            if index:
                split_words = lower_words.split(' ')
                singleword = ''.join(lower_words)

                query_list = [*split_words, singleword]

                index_match_keys = set()
                for key in index:
                    for query in query_list:
                        if query in key:
                            index_match_keys.update(index[key])

                if not index_match_keys:
                    self.logger.info("Query '{}' was not found in the suggestion's index".format(words))
                    res.installed.extend(installed_matches)
                else:
                    if not os.path.exists(SUGGESTIONS_CACHE_FILE):
                        # if the suggestions cache was not found, it will not be possible to retrieve the matched apps
                        # so only the installed matches will be returned
                        self.logger.warning("Suggestion cached file {} was not found".format(SUGGESTIONS_CACHE_FILE))
                        res.installed.extend(installed_matches)
                    else:
                        with open(SUGGESTIONS_CACHE_FILE) as f:
                            cached_suggestions = yaml.safe_load(f.read())

                        if not cached_suggestions:
                            # if no suggestion is found, it will not be possible to retrieve the matched apps
                            # so only the installed matches will be returned
                            self.logger.warning("No suggestion found in {}".format(SUGGESTIONS_CACHE_FILE))
                            res.installed.extend(installed_matches)
                        else:
                            matched_suggestions = [cached_suggestions[key] for key in index_match_keys if cached_suggestions.get(key)]

                            if not matched_suggestions:
                                self.logger.warning("No suggestion found for the search index keys: {}".format(index_match_keys))
                                res.installed.extend(installed_matches)
                            else:
                                matched_suggestions.sort(key=lambda s: s.get('priority', 0), reverse=True)

                                thread_config.join()
                                env_settings = self.env_updater.read_settings(web_config=web_config, cache=True)

                                if installed_matches:
                                    # checking if any of the installed matches is one of the matched suggestions

                                    for sug in matched_suggestions:
                                        sug_url = sug['url'][0:-1] if sug['url'].endswith('/') else sug['url']

                                        found = [i for i in installed_matches if sug_url in {i.url, i.get_source_url()}]

                                        if found:
                                            res.installed.extend(found)
                                        else:
                                            res.new.append(self._map_suggestion(sug, env_settings).package)

                                else:
                                    for sug in matched_suggestions:
                                        res.new.append(self._map_suggestion(sug, env_settings).package)

        res.total += len(res.installed)
        res.total += len(res.new)

        if res.new:
            thread_config.join()

            if web_config['environment']['electron']['version']:
                for app in res.new:
                    app.version = str(web_config['environment']['electron']['version'])
                    app.latest_version = app.version

        return res

    def read_installed(self, disk_loader: Optional[DiskCacheLoader], limit: int = -1, only_apps: bool = False, pkg_types: Set[Type[SoftwarePackage]] = None, internet_available: bool = True) -> SearchResult:
        res = SearchResult([], [], 0)

        if os.path.exists(INSTALLED_PATH):
            for data_path in glob.glob('{}/*/*data.yml'.format(INSTALLED_PATH)):
                with open(data_path, 'r') as f:
                    res.installed.append(WebApplication(installed=True, **yaml.safe_load(f.read())))
                    res.total += 1

        return res

    def downgrade(self, pkg: SoftwarePackage, root_password: Optional[str], handler: ProcessWatcher) -> bool:
        pass

    def upgrade(self, requirements: UpgradeRequirements, root_password: Optional[str], watcher: ProcessWatcher) -> bool:
        pass

    def uninstall(self, pkg: WebApplication, root_password: Optional[str], watcher: ProcessWatcher, disk_loader: DiskCacheLoader) -> TransactionResult:
        self.logger.info("Checking if {} installation directory {} exists".format(pkg.name, pkg.installation_dir))

        if not os.path.exists(pkg.installation_dir):
            watcher.show_message(title=self.i18n['error'],
                                 body=self.i18n['web.uninstall.error.install_dir.not_found'].format(bold(pkg.installation_dir)),
                                 type_=MessageType.ERROR)
            return TransactionResult.fail()

        self.logger.info("Removing {} installation directory {}".format(pkg.name, pkg.installation_dir))
        try:
            shutil.rmtree(pkg.installation_dir)
        except:
            watcher.show_message(title=self.i18n['error'],
                                 body=self.i18n['web.uninstall.error.remove'].format(bold(pkg.installation_dir)),
                                 type_=MessageType.ERROR)
            traceback.print_exc()
            return TransactionResult.fail()

        self.logger.info("Checking if {} desktop entry file {} exists".format(pkg.name, pkg.desktop_entry))
        if os.path.exists(pkg.desktop_entry):
            try:
                os.remove(pkg.desktop_entry)
            except:
                watcher.show_message(title=self.i18n['error'],
                                     body=self.i18n['web.uninstall.error.remove'].format(bold(pkg.desktop_entry)),
                                     type_=MessageType.ERROR)
                traceback.print_exc()

        autostart_path = pkg.get_autostart_path()
        if os.path.exists(autostart_path):
            try:
                os.remove(autostart_path)
            except:
                watcher.show_message(title=self.i18n['error'],
                                     body=self.i18n['web.uninstall.error.remove'].format(bold(autostart_path)),
                                     type_=MessageType.WARNING)
                traceback.print_exc()

        config_path = pkg.get_config_dir()

        if config_path and os.path.exists(config_path):
            try:
                shutil.rmtree(config_path)
            except:
                watcher.show_message(title=self.i18n['error'],
                                     body=self.i18n['web.uninstall.error.remove'].format(bold(config_path)),
                                     type_=MessageType.WARNING)
                traceback.print_exc()

        self.logger.info(f"Checking for Javascript fix file associated with {pkg.name}")
        fix_path = FIX_FILE_PATH.format(app_id=pkg.id, electron_branch=self._map_electron_branch(pkg.version))

        if os.path.isfile(fix_path):
            self.logger.info(f"Removing fix file '{fix_path}'")
            try:
                os.remove(fix_path)
            except:
                self.logger.error(f"Could not remove fix file '{fix_path}'")
                traceback.print_exc()
                watcher.show_message(title=self.i18n['error'],
                                     body=self.i18n['web.uninstall.error.remove'].format(bold(fix_path)),
                                     type_=MessageType.WARNING)

        return TransactionResult(success=True, installed=None, removed=[pkg])

    def get_managed_types(self) -> Set[Type[SoftwarePackage]]:
        return {WebApplication}

    def get_info(self, pkg: WebApplication) -> dict:
        if pkg.installed:
            info = {'0{}_{}'.format(idx + 1, att): getattr(pkg, att) for idx, att in enumerate(('url', 'description', 'version', 'categories', 'installation_dir', 'desktop_entry'))}
            info['07_exec_file'] = pkg.get_exec_path()
            info['08_icon_path'] = pkg.get_disk_icon_path()

            if os.path.exists(pkg.installation_dir):
                info['09_size'] = get_human_size_str(get_dir_size(pkg.installation_dir))

            config_dir = pkg.get_config_dir()

            if config_dir:
                info['10_config_dir'] = config_dir

            if info.get('04_categories'):
                info['04_categories'] = [self.i18n[c.lower()].capitalize() for c in info['04_categories']]

            return info
        else:
            return {'0{}_{}'.format(idx + 1, att): getattr(pkg, att) for idx, att in enumerate(('url', 'description', 'version', 'categories'))}

    def get_history(self, pkg: SoftwarePackage) -> PackageHistory:
        pass

    def _ask_install_options(self, app: WebApplication, watcher: ProcessWatcher, pre_validated: bool) -> Tuple[bool, List[str]]:
        watcher.change_substatus(self.i18n['web.install.substatus.options'])

        inp_url = TextInputComponent(label=self.i18n['address'].capitalize() + ' (URL)', capitalize_label=False, value=app.url,
                                     read_only=pre_validated, placeholder=f"({self.i18n['example.short']}: https://myapp123.com)")
        inp_name = TextInputComponent(label=self.i18n['name'], value=app.name)
        inp_desc = TextInputComponent(label=self.i18n['description'], value=app.description)

        cat_ops = [InputOption(label=self.i18n['web.install.option.category.none'].capitalize(), value=0)]
        cat_ops.extend([InputOption(label=self.i18n.get('category.{}'.format(c.lower()), c).capitalize(), value=c) for c in self.context.default_categories])

        def_cat = cat_ops[0]

        if app.categories:
            for opt in cat_ops:
                if opt.value == app.categories[0]:
                    def_cat = opt
                    break
        else:
            for op in cat_ops:
                if op.value == 'Network':
                    def_cat = op
                    break

        inp_cat = SingleSelectComponent(label=self.i18n['category'], type_=SelectViewType.COMBO, options=cat_ops, default_option=def_cat)
        op_wv = InputOption(id_='widevine', label=self.i18n['web.install.option.widevine.label'] + ' (DRM)', value="--widevine", tooltip=self.i18n['web.install.option.widevine.tip'])
        tray_op_off = InputOption(id_='tray_off', label=self.i18n['web.install.option.tray.off.label'], value=0, tooltip=self.i18n['web.install.option.tray.off.tip'])
        tray_op_default = InputOption(id_='tray_def', label=self.i18n['web.install.option.tray.default.label'], value='--tray', tooltip=self.i18n['web.install.option.tray.default.tip'])
        tray_op_min = InputOption(id_='tray_min', label=self.i18n['web.install.option.tray.min.label'], value='--tray=start-in-tray', tooltip=self.i18n['web.install.option.tray.min.tip'])

        tray_opts = [tray_op_off, tray_op_default, tray_op_min]
        def_tray_opt = None

        if app.preset_options:
            for opt in tray_opts:
                if opt.id in app.preset_options:
                    def_tray_opt = opt
                    break

        inp_tray = SingleSelectComponent(type_=SelectViewType.COMBO,
                                         options=tray_opts,
                                         default_option=def_tray_opt,
                                         label=self.i18n['web.install.option.tray.label'])

        icon_op_ded = InputOption(id_='icon_ded', label=self.i18n['web.install.option.wicon.deducted.label'], value=0,
                                  tooltip=self.i18n['web.install.option.wicon.deducted.tip'].format('Nativefier'))

        if pre_validated:
            icon_op_disp = InputOption(id_='icon_disp', label=self.i18n['web.install.option.wicon.displayed.label'],
                                       value=1, tooltip=self.i18n['web.install.option.wicon.displayed.tip'])
        else:
            icon_op_disp = None

        inp_icon = SingleSelectComponent(type_=SelectViewType.COMBO,
                                         options=[op for op in (icon_op_ded, icon_op_disp) if op],
                                         default_option=icon_op_disp if app.icon_url and app.save_icon else icon_op_ded,
                                         label=self.i18n['web.install.option.wicon.label'])

        icon_chooser = FileChooserComponent(allowed_extensions={'png', 'svg', 'ico', 'jpg', 'jpeg'}, label=self.i18n['web.install.option.icon.label'])

        form_1 = FormComponent(components=[inp_url, inp_name, inp_desc, inp_cat, inp_icon, icon_chooser, inp_tray], label=self.i18n['web.install.options.basic'].capitalize())

        op_single = InputOption(id_='single', label=self.i18n['web.install.option.single.label'], value="--single-instance", tooltip=self.i18n['web.install.option.single.tip'])
        op_max = InputOption(id_='max', label=self.i18n['web.install.option.max.label'], value="--maximize", tooltip=self.i18n['web.install.option.max.tip'])
        op_fs = InputOption(id_='fullscreen', label=self.i18n['web.install.option.fullscreen.label'], value="--full-screen", tooltip=self.i18n['web.install.option.fullscreen.tip'])
        op_nframe = InputOption(id_='no_frame', label=self.i18n['web.install.option.noframe.label'], value="--hide-window-frame", tooltip=self.i18n['web.install.option.noframe.tip'])
        op_allow_urls = InputOption(id_='allow_urls', label=self.i18n['web.install.option.allow_urls.label'], value='--internal-urls=.*', tooltip=self.i18n['web.install.option.allow_urls.tip'])
        op_ncache = InputOption(id_='no_cache', label=self.i18n['web.install.option.nocache.label'], value="--clear-cache", tooltip=self.i18n['web.install.option.nocache.tip'])
        op_insecure = InputOption(id_='insecure', label=self.i18n['web.install.option.insecure.label'], value="--insecure", tooltip=self.i18n['web.install.option.insecure.tip'])
        op_igcert = InputOption(id_='ignore_certs', label=self.i18n['web.install.option.ignore_certificate.label'], value="--ignore-certificate", tooltip=self.i18n['web.install.option.ignore_certificate.tip'])

        adv_opts = [op_single, op_allow_urls, op_wv, op_max, op_fs, op_nframe, op_ncache, op_insecure, op_igcert]
        def_adv_opts = {op_single, op_allow_urls}

        if app.preset_options:
            for opt in adv_opts:
                if opt.id in app.preset_options:
                    def_adv_opts.add(opt)

        check_options = MultipleSelectComponent(options=adv_opts, default_options=def_adv_opts, label=self.i18n['web.install.options.advanced'].capitalize())

        install_ = watcher.request_confirmation(title=self.i18n['web.install.options_dialog.title'],
                                                body=None,
                                                components=[form_1, check_options],
                                                confirmation_label=self.i18n['continue'].capitalize(),
                                                deny_label=self.i18n['cancel'].capitalize())

        if install_:
            if not pre_validated:
                typed_url = inp_url.get_value().strip()

                if not typed_url or not self._request_url(typed_url):
                    watcher.show_message(title=self.i18n['error'].capitalize(),
                                         type_=MessageType.ERROR,
                                         body=self.i18n['web.custom_action.install_app.invalid_url'].format(URL='(URL)',
                                                                                                            url=bold(f'"{inp_url.get_value()}"')))
                    return False, []
                else:
                    app.url = typed_url

            selected = []

            if check_options.values:
                selected.extend(check_options.get_selected_values())

            tray_mode = inp_tray.get_selected()
            if tray_mode is not None and tray_mode != 0:
                selected.append(tray_mode)

            custom_name = inp_name.get_value()

            if custom_name:
                app.name = custom_name

            custom_desc = inp_desc.get_value()

            if custom_desc:
                app.description = inp_desc.get_value()

            cat = inp_cat.get_selected()

            if cat != 0:
                app.categories = [cat]

            if icon_chooser.file_path:
                app.set_custom_icon(icon_chooser.file_path)
                selected.append('--icon={}'.format(icon_chooser.file_path))

            app.save_icon = inp_icon.value == icon_op_disp if icon_op_disp else False

            return True, selected

        return False, []

    def _gen_app_id(self, name: str) -> Tuple[str, str]:

        treated_name = RE_SYMBOLS_SPLIT.sub('-', name.lower().strip())
        config_path = '{}/.config'.format(Path.home())

        counter = 0
        while True:
            app_id = '{}{}'.format(treated_name, '-{}'.format(counter) if counter else '')

            if not os.path.exists('{}/{}'.format(INSTALLED_PATH, app_id)):
                # checking if there is no config folder associated with the id
                if os.path.exists(config_path):
                    if not glob.glob('{}/{}-nativefier-*'.format(config_path, app_id)):
                        return app_id, treated_name

            counter += 1

    def _gen_desktop_entry_path(self, app_id: str) -> str:
        base_id = app_id
        counter = 1

        while True:
            desk_path = DESKTOP_ENTRY_PATH_PATTERN.format(name=base_id)
            if not os.path.exists(desk_path):
                return desk_path
            else:
                base_id = '{}_{}'.format(app_id, counter)
                counter += 1

    def _ask_update_permission(self, to_update: List[EnvironmentComponent], watcher: ProcessWatcher) -> bool:

        icon = resource.get_path('img/web.png', ROOT_DIR)
        opts = [InputOption(label='{} ( {} )'.format(f.name, f.size or '?'),
                            tooltip=f.url, icon_path=icon, read_only=True, value=f.name) for f in to_update]

        comps = MultipleSelectComponent(label=None, options=opts, default_options=set(opts))

        return watcher.request_confirmation(title=self.i18n['web.install.env_update.title'],
                                            body=self.i18n['web.install.env_update.body'],
                                            components=[comps],
                                            confirmation_label=self.i18n['continue'].capitalize(),
                                            deny_label=self.i18n['cancel'].capitalize())

    def _download_suggestion_icon(self, pkg: WebApplication, app_dir: str) -> Tuple[str, bytes]:
        try:
            if self.http_client.exists(pkg.icon_url, session=False):
                icon_path = '{}/{}'.format(app_dir, pkg.icon_url.split('/')[-1])

                try:
                    res = self.http_client.get(pkg.icon_url, session=False)
                    if not res:
                        self.logger.info('Could not download the icon {}'.format(pkg.icon_url))
                    else:
                        return icon_path, res.content
                except:
                    self.logger.error("An exception has happened when downloading {}".format(pkg.icon_url))
                    traceback.print_exc()
            else:
                self.logger.warning('Could no retrieve the icon {} defined for the suggestion {}'.format(pkg.icon_url, pkg.name))
        except:
            self.logger.warning('An exception happened when trying to retrieve the icon {} for the suggestion {}'.format(pkg.icon_url,
                                                                                                         pkg.name))
            traceback.print_exc()

    def _install(self, pkg: WebApplication, install_options: List[str], watcher: ProcessWatcher) -> TransactionResult:
        widevine_support = '--widevine' in install_options

        watcher.change_substatus(self.i18n['web.env.checking'])
        handler = ProcessHandler(watcher)

        web_config = self.configman.get_config()
        env_settings = self.env_updater.read_settings(web_config=web_config)

        if web_config['environment']['system'] and not nativefier.is_available():
            watcher.show_message(title=self.i18n['error'].capitalize(),
                                 body=self.i18n['web.install.global_nativefier.unavailable'].format(
                                     n=bold('Nativefier'), app=bold(pkg.name)) + '.',
                                 type_=MessageType.ERROR)
            return TransactionResult(success=False, installed=[], removed=[])

        env_components = self.env_updater.check_environment(app=pkg, local_config=web_config, env=env_settings,
                                                            is_x86_x64_arch=self.context.is_system_x86_64(),
                                                            widevine=widevine_support)

        comps_to_update = [c for c in env_components if c.update]

        if comps_to_update and not self._ask_update_permission(comps_to_update, watcher):
            return TransactionResult(success=False, installed=[], removed=[])

        if not self.env_updater.update(components=comps_to_update, handler=handler):
            watcher.show_message(title=self.i18n['error'], body=self.i18n['web.env.error'].format(bold(pkg.name)),
                                 type_=MessageType.ERROR)
            return TransactionResult(success=False, installed=[], removed=[])

        Path(INSTALLED_PATH).mkdir(parents=True, exist_ok=True)

        app_id, treated_name = self._gen_app_id(pkg.name)
        pkg.id = app_id
        app_dir = '{}/{}'.format(INSTALLED_PATH, app_id)

        watcher.change_substatus(self.i18n['web.install.substatus.checking_fixes'])

        electron_version = str(next((c for c in env_components if c.id == 'electron')).version)

        url_domain, electron_branch = self._strip_url_protocol(pkg.url), self._map_electron_branch(electron_version)
        fix = self._get_fix_for(url_domain=url_domain, electron_branch=electron_branch)

        if fix:
            # just adding the fix as an installation option. The file will be written later
            fix_log = f'Fix found for {pkg.url} (Electron: {electron_version})'
            self.logger.info(fix_log)
            watcher.print(fix_log)

            fix_path = FIX_FILE_PATH.format(app_id=pkg.id, electron_branch=electron_branch)
            Path(os.path.dirname(fix_path)).mkdir(parents=True, exist_ok=True)

            install_options.append(f'--inject={fix_path}')
            Path(FIX_FILE_PATH).mkdir(exist_ok=True, parents=True)

            self.logger.info(f'Writing JS fix at {fix_path}')
            with open(fix_path, 'w+') as f:
                f.write(fix)

        # if a custom icon is defined for an app suggestion:
        icon_path, icon_bytes = None, None
        if pkg.icon_url and pkg.save_icon and not {o for o in install_options if o.startswith('--icon')}:
            download = self._download_suggestion_icon(pkg, app_dir)

            if download and download[1]:
                icon_path, icon_bytes = download[0], download[1]
                pkg.custom_icon = icon_path

                # writing the icon in a temporary folder to be used by the nativefier process
                temp_icon_path = '{}/{}'.format(TEMP_PATH, pkg.icon_url.split('/')[-1])
                install_options.append('--icon={}'.format(temp_icon_path))

                self.logger.info("Writing a temp suggestion icon at {}".format(temp_icon_path))

                Path(os.path.dirname(temp_icon_path)).mkdir(parents=True, exist_ok=True)

                with open(temp_icon_path, 'wb+') as f:
                    f.write(icon_bytes)

        custom_props = self._get_custom_properties(url_domain=url_domain, electron_branch=electron_branch)

        if custom_props:
            for prop, val in custom_props.items():
                if hasattr(pkg, prop):
                    try:
                        setattr(pkg, prop, val)
                        self.logger.info(
                            f"Using custom installation property '{prop}' ({val if val else '<null>'}) for '{url_domain}' "
                            f"(Electron: {electron_version})")
                    except:
                        self.logger.error(
                            f"Could not set the custom installation property '{prop}' ({val if val else '<null>'}) "
                            f"for '{url_domain}' (Electron: {electron_version})")

        watcher.change_substatus(self.i18n['web.install.substatus.call_nativefier'].format(bold('nativefier')))

        installed = handler.handle_simple(nativefier.install(url=pkg.url, name=app_id, output_dir=app_dir,
                                                             electron_version=electron_version if not widevine_support else None,
                                                             system=bool(web_config['environment']['system']),
                                                             cwd=INSTALLED_PATH,
                                                             user_agent=pkg.user_agent,
                                                             extra_options=install_options))

        if not installed:
            msg = '{}.{}.'.format(self.i18n['wen.install.error'].format(bold(pkg.name)),
                                  self.i18n['web.install.nativefier.error.unknown'].format(
                                      bold(self.i18n['details'].capitalize())))
            watcher.show_message(title=self.i18n['error'], body=msg, type_=MessageType.ERROR)
            return TransactionResult(success=False, installed=[], removed=[])

        inner_dir = os.listdir(app_dir)

        if not inner_dir:
            msg = '{}.{}.'.format(self.i18n['wen.install.error'].format(bold(pkg.name)),
                                  self.i18n['web.install.nativefier.error.inner_dir'].format(bold(app_dir)))
            watcher.show_message(title=self.i18n['error'], body=msg, type_=MessageType.ERROR)
            return TransactionResult(success=False, installed=[], removed=[])

        # bringing the inner app folder to the 'installed' folder level:
        inner_dir = '{}/{}'.format(app_dir, inner_dir[0])
        temp_dir = '{}/tmp_{}'.format(INSTALLED_PATH, treated_name)
        os.rename(inner_dir, temp_dir)
        shutil.rmtree(app_dir)
        os.rename(temp_dir, app_dir)

        # persisting the custom suggestion icon in the defitive directory
        if icon_bytes:
            self.logger.info("Writting the final custom suggestion icon at {}".format(icon_path))
            with open(icon_path, 'wb+') as f:
                f.write(icon_bytes)

        pkg.installation_dir = app_dir

        version_path = '{}/version'.format(app_dir)

        if os.path.exists(version_path):
            with open(version_path, 'r') as f:
                pkg.version = f.read().strip()
                pkg.latest_version = pkg.version

        watcher.change_substatus(self.i18n['web.install.substatus.shortcut'])

        try:
            package_info_path = '{}/resources/app/package.json'.format(pkg.installation_dir)
            with open(package_info_path) as f:
                package_info_path = json.loads(f.read())
                pkg.package_name = package_info_path['name']
        except:
            self.logger.info("Could not read the the package info from '{}'".format(package_info_path))
            traceback.print_exc()

        desktop_entry_path = self._gen_desktop_entry_path(app_id)

        entry_content = self._gen_desktop_entry_content(pkg)

        Path(DESKTOP_ENTRIES_DIR).mkdir(parents=True, exist_ok=True)

        with open(desktop_entry_path, 'w+') as f:
            f.write(entry_content)

        pkg.desktop_entry = desktop_entry_path

        autostart_file = pkg.get_autostart_path()

        if autostart_file and '--tray=start-in-tray' in install_options:
            Path(os.path.dirname(autostart_file)).mkdir(parents=True, exist_ok=True)

            with open(autostart_file, 'w+') as f:
                f.write(entry_content)

            self.logger.info(f"Autostart file created '{autostart_file}'")

        if install_options:
            pkg.options_set = install_options

        return TransactionResult(success=True, installed=[pkg], removed=[])

    def install(self, pkg: WebApplication, root_password: Optional[str], disk_loader: DiskCacheLoader, watcher: ProcessWatcher) -> TransactionResult:
        continue_install, install_options = self._ask_install_options(pkg, watcher, pre_validated=True)

        if not continue_install:
            return TransactionResult(success=False, installed=[], removed=[])

        return self._install(pkg, install_options, watcher)

    def install_app(self, root_password: Optional[str], watcher: ProcessWatcher) -> bool:
        pkg = WebApplication()
        continue_install, install_options = self._ask_install_options(pkg, watcher, pre_validated=False)

        if not continue_install:
            return False

        if self._install(pkg, install_options, watcher).success:
            self.serialize_to_disk(pkg, icon_bytes=None, only_icon=False)
            return True

        return False

    def _gen_desktop_entry_content(self, pkg: WebApplication) -> str:
        return """
        [Desktop Entry]
        Type=Application
        Name={name} (web)
        Comment={desc}
        Icon={icon}
        Exec={exec_path}
        {categories}
        {wmclass}
        """.format(name=pkg.name, exec_path=pkg.get_command(),
                   desc=pkg.description or pkg.url, icon=pkg.get_disk_icon_path(),
                   categories='Categories={}'.format(';'.join(pkg.categories)) if pkg.categories else '',
                   wmclass="StartupWMClass={}".format(pkg.package_name) if pkg.package_name else '')

    def is_enabled(self) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

    def can_work(self) -> Tuple[bool, Optional[str]]:
        if not BS4_AVAILABLE:
            return False, self.i18n['missing_dep'].format(dep=bold('python3-beautifulsoup4'))

        if not LXML_AVAILABLE:
            return False, self.i18n['missing_dep'].format(dep=bold('python3-lxml'))

        config = self.configman.get_config()
        use_system_env = config['environment']['system']

        if not use_system_env:
            return True, None

        if not nativefier.is_available():
            return False, self.i18n['missing_dep'].format(dep=bold('nativefier'))

        return True, None

    def requires_root(self, action: SoftwareAction, pkg: SoftwarePackage) -> bool:
        return False

    def _assign_suggestions(self, suggestions: dict):
        self.suggestions = suggestions

    def prepare(self, task_manager: TaskManager, root_password: Optional[str], internet_available: bool):
        create_config = CreateConfigFile(taskman=task_manager, configman=self.configman, i18n=self.i18n,
                                         task_icon_path=get_icon_path(), logger=self.logger)
        create_config.start()

        if internet_available:
            UpdateEnvironmentSettings(env_updater=EnvironmentUpdater(logger=self.context.logger,
                                                                     i18n=self.i18n,
                                                                     http_client=self.http_client,
                                                                     file_downloader=self.context.file_downloader,
                                                                     taskman=task_manager),
                                      taskman=task_manager,
                                      create_config=create_config,
                                      i18n=self.i18n).start()

        self.suggestions_loader = SuggestionsLoader(manager=SuggestionsManager(logger=self.logger,
                                                                               http_client=self.http_client,
                                                                               i18n=self.i18n),
                                                    logger=self.logger,
                                                    i18n=self.i18n,
                                                    taskman=task_manager,
                                                    suggestions_callback=self._assign_suggestions,
                                                    create_config=create_config,
                                                    internet_connection=internet_available)
        self.suggestions_loader.start()

        SearchIndexGenerator(taskman=task_manager,
                             idxman=self.idxman,
                             suggestions_loader=self.suggestions_loader,
                             i18n=self.i18n,
                             logger=self.logger).start()

    def list_updates(self, internet_available: bool) -> List[PackageUpdate]:
        pass

    def list_warnings(self, internet_available: bool) -> Optional[List[str]]:
        pass

    def _fill_suggestion(self, app: WebApplication):
        soup_map = self._map_url(app.url)

        if soup_map:
            soup, res = soup_map[0], soup_map[1]

            app.url = res.url

            if app.url.endswith('/'):
                app.url = app.url[0:-1]

            if not app.name:
                app.name = self._get_app_name(app.url, soup)

            if not app.description:
                app.description = self._get_app_description(app.url, soup)

            try:
                find_url = not app.icon_url or (app.icon_url and not self.http_client.exists(app.icon_url, session=False))
            except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout):
                find_url = None

            if find_url:
                app.icon_url = self._get_app_icon_url(app.url, soup)

        app.status = PackageStatus.READY

    def _map_suggestion(self, suggestion: dict, env_settings: Optional[dict]) -> PackageSuggestion:
        app = WebApplication(name=suggestion.get('name'),
                             url=suggestion.get('url'),
                             icon_url=suggestion.get('icon_url'),
                             categories=[suggestion['category']] if suggestion.get('category') else None,
                             preset_options=suggestion.get('options'),
                             save_icon=suggestion.get('save_icon', False),
                             user_agent=suggestion.get('user_agent'))

        app.set_version(suggestion.get('version'))

        description = suggestion.get('description')

        if isinstance(description, dict):
            app.description = description.get(self.i18n.current_key, description.get(self.i18n.default_key))
        elif isinstance(description, str):
            app.description = description

        if not app.version and env_settings and env_settings.get('electron'):
            app.version = env_settings['electron']['version']
            app.latest_version = app.version

        app.status = PackageStatus.LOADING_DATA

        Thread(target=self._fill_suggestion, args=(app,), daemon=True).start()

        return PackageSuggestion(priority=SuggestionPriority(suggestion['priority']), package=app)

    def _fill_config_async(self, output: dict):
        output.update(self.configman.get_config())

    def list_suggestions(self, limit: int, filter_installed: bool) -> List[PackageSuggestion]:
        web_config = {}

        thread_config = Thread(target=self._fill_config_async, args=(web_config,))
        thread_config.start()

        if self.suggestions:
            suggestions = self.suggestions
        elif self.suggestions_loader:
            self.suggestions_loader.join(5)
            suggestions = self.suggestions
        else:
            suggestions = SuggestionsManager(logger=self.logger, http_client=self.http_client, i18n=self.i18n).download()

        # cleaning memory
        self.suggestions_loader = None
        self.suggestions = None

        if suggestions:
            suggestion_list = list(suggestions.values())
            suggestion_list.sort(key=lambda s: s.get('priority', 0), reverse=True)

            if filter_installed:
                installed = {self._strip_url_protocol(i.url) for i in self.read_installed(disk_loader=None).installed}
            else:
                installed = None

            env_settings, res = None, []

            for s in suggestion_list:
                if limit <= 0 or len(res) < limit:
                    if installed:
                        surl = self._strip_url_protocol(s['url'])

                        if surl in installed:
                            continue

                    if env_settings is None:  # reading settings if not already loaded
                        thread_config.join()
                        env_settings = self.env_updater.read_settings(web_config=web_config)

                    res.append(self._map_suggestion(s, env_settings))
                else:
                    break

            if res:
                if env_settings:
                    for s in res:
                        s.package.version = env_settings['electron']['version']
                        s.package.latest_version = s.package.version

                thread_config.join()
                if web_config and web_config['environment']['electron']['version']:
                    for s in res:
                        s.package.version = str(web_config['environment']['electron']['version'])
                        s.package.latest_version = s.package.version

            return res

    def execute_custom_action(self, action: CustomSoftwareAction, pkg: SoftwarePackage, root_password: Optional[str], watcher: ProcessWatcher) -> bool:
        pass

    def is_default_enabled(self) -> bool:
        return True

    def launch(self, pkg: WebApplication):
        subprocess.Popen(args=[pkg.get_command()], shell=True, env={**os.environ})

    def clear_data(self, logs: bool = True):
        if os.path.exists(ENV_PATH):
            if logs:
                print('[bauh][web] Deleting directory {}'.format(ENV_PATH))

            try:
                shutil.rmtree(ENV_PATH)
                if logs:
                    print('{}[bauh][web] Directory {} deleted{}'.format(Fore.YELLOW, ENV_PATH, Fore.RESET))
            except:
                if logs:
                    print('{}[bauh][web] An exception has happened when deleting {}{}'.format(Fore.RED, ENV_PATH, Fore.RESET))
                    traceback.print_exc()

    def get_settings(self, screen_width: int, screen_height: int) -> Optional[ViewComponent]:
        web_config = self.configman.get_config()
        max_width = floor(screen_width * 0.15)

        input_electron = TextInputComponent(label=self.i18n['web.settings.electron.version.label'],
                                            value=web_config['environment']['electron']['version'],
                                            tooltip=self.i18n['web.settings.electron.version.tooltip'],
                                            placeholder='{}: 7.1.0'.format(self.i18n['example.short']),
                                            max_width=max_width,
                                            id_='electron_branch')

        native_opts = [
            InputOption(label=self.i18n['web.settings.nativefier.env'].capitalize(), value=False, tooltip=self.i18n['web.settings.nativefier.env.tooltip'].format(app=self.context.app_name)),
            InputOption(label=self.i18n['web.settings.nativefier.system'].capitalize(), value=True, tooltip=self.i18n['web.settings.nativefier.system.tooltip'])
        ]

        select_nativefier = SingleSelectComponent(label="Nativefier",
                                                  options=native_opts,
                                                  default_option=[o for o in native_opts if o.value == web_config['environment']['system']][0],
                                                  type_=SelectViewType.COMBO,
                                                  tooltip=self.i18n['web.settings.nativefier.tip'],
                                                  max_width=max_width,
                                                  id_='nativefier')

        env_settings_exp = TextInputComponent(label=self.i18n['web.settings.cache_exp'],
                                              tooltip=self.i18n['web.settings.cache_exp.tip'],
                                              capitalize_label=False,
                                              value=int(web_config['environment']['cache_exp']) if isinstance(web_config['environment']['cache_exp'], int) else '',
                                              only_int=True,
                                              max_width=max_width,
                                              id_='web_cache_exp')

        sugs_exp = TextInputComponent(label=self.i18n['web.settings.suggestions.cache_exp'],
                                      tooltip=self.i18n['web.settings.suggestions.cache_exp.tip'],
                                      capitalize_label=False,
                                      value=int(web_config['suggestions']['cache_exp']) if isinstance(
                                          web_config['suggestions']['cache_exp'], int) else '',
                                      only_int=True,
                                      max_width=max_width,
                                      id_='web_sugs_exp')

        form_env = FormComponent(label=self.i18n['web.settings.nativefier.env'].capitalize(),
                                 components=[input_electron, select_nativefier, env_settings_exp, sugs_exp])

        return PanelComponent([form_env])

    def save_settings(self, component: PanelComponent) -> Tuple[bool, Optional[List[str]]]:
        web_config = self.configman.get_config()

        form_env = component.components[0]

        web_config['environment']['electron']['version'] = str(form_env.get_component('electron_branch').get_value()).strip()

        if len(web_config['environment']['electron']['version']) == 0:
            web_config['environment']['electron']['version'] = None

        system_nativefier = form_env.get_component('nativefier').get_selected()

        if system_nativefier and not nativefier.is_available():
            return False, [self.i18n['web.settings.env.nativefier.system.not_installed'].format('Nativefier')]

        web_config['environment']['system'] = system_nativefier
        web_config['environment']['cache_exp'] = form_env.get_component('web_cache_exp').get_int_value()
        web_config['suggestions']['cache_exp'] = form_env.get_component('web_sugs_exp').get_int_value()

        try:
            self.configman.save_config(web_config)
            return True, None
        except:
            return False, [traceback.format_exc()]

    def gen_custom_actions(self) -> Generator[CustomSoftwareAction, None, None]:
        if self._custom_actions is None:
            self._custom_actions = (
                CustomSoftwareAction(i18n_label_key='web.custom_action.install_app',
                                     i18n_status_key='web.custom_action.install_app.status',
                                     i18n_description_key='web.custom_action.install_app.desc',
                                     manager=self,
                                     manager_method='install_app',
                                     icon_path=resource.get_path('img/web.svg', ROOT_DIR),
                                     requires_root=False,
                                     refresh=True,
                                     requires_confirmation=False),
                CustomSoftwareAction(i18n_label_key='web.custom_action.clean_env',
                                     i18n_status_key='web.custom_action.clean_env.status',
                                     i18n_description_key='web.custom_action.clean_env.desc',
                                     manager=self,
                                     manager_method='clean_environment',
                                     icon_path=resource.get_path('img/web.svg', ROOT_DIR),
                                     requires_root=False,
                                     refresh=False)
            )

        yield from self._custom_actions

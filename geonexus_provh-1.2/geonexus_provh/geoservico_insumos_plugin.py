# -*- coding: utf-8 -*-
"""GeoNexus PROvh — versão 1.2."""
import json
import base64
import os
import re
import uuid
import zipfile
from qgis.PyQt.QtXml import QDomDocument
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote, unquote_plus, urlencode, quote

from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QUrl, QUrlQuery, QStandardPaths
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QPushButton, QProgressBar, QMessageBox, QCheckBox,
    QFrame, QScrollArea, QWidget, QListWidget, QGroupBox
)
from qgis.core import QgsProject, QgsApplication, QgsAuthMethodConfig, QgsNetworkAccessManager, QgsMessageLog, Qgis, QgsVectorLayer, QgsCoordinateTransform, QgsFillSymbol

STATES = [
    ('AMAPÁ', 'flag_amapa.png'), ('PARÁ', 'flag_para.png'),
    ('BAHIA', 'flag_bahia.png'), ('GOIÁS', 'flag_goias.png'),
    ('TOCANTINS', 'flag_tocantins.png'), ('MATO GROSSO', 'flag_matogrosso.png'),
    ('RONDÔNIA', 'flag_rondonia.png'),
]
STYLE = """
QDialog {background:#f4f7fb; color:#17314c; font-size:12px;}
QLabel#title {font-size:21px; font-weight:700; color:#123956;}
QLabel#muted {color:#667b8f;}
QLineEdit {background:white; border:1px solid #cbd7e3; border-radius:7px; padding:9px;}
QLineEdit:focus {border:1px solid #267a9e;}
QPushButton {background:#145d79; color:white; border:0; border-radius:7px; padding:10px; font-weight:600;}
QPushButton:hover {background:#197a9d;}
QPushButton:disabled {background:#aebec8;}
QPushButton#state {background:white; color:#17314c; border:1px solid #d7e2eb; text-align:left; padding:13px;}
QPushButton#state:hover {border:1px solid #267a9e; background:#e8f5fa;}
QFrame#panel {background:white; border:1px solid #e0e8ef; border-radius:10px;}
QProgressBar {border:1px solid #d7e2eb; border-radius:5px; text-align:center; height:18px; background:white;}
QProgressBar::chunk {background:#218b94; border-radius:4px;}
"""


API_BASE = 'https://geonexusweb.datacampo.cloud/api/v1/qgis'
STATE_UF = {'AMAPÁ': 'AP', 'PARÁ': 'PA', 'BAHIA': 'BA', 'GOIÁS': 'GO',
            'TOCANTINS': 'TO', 'MATO GROSSO': 'MT', 'RONDÔNIA': 'RO'}
GATEWAY_URL = API_BASE + '/wms'


class GeoservicoInsumosPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.settings = QSettings('FORGEGEO', 'AnaliseCAR')
        # Purge plaintext credentials from 1.16 at initialization.
        self.settings.remove('password')
        self.api_base_url = API_BASE
        self._basic_authorization = None
        self._wms_preprocessor_installed = False
        self._wms_preprocessor = None
        self.machine_id = self.settings.value('machine_id', '') or str(uuid.uuid4())
        self.settings.setValue('machine_id', self.machine_id)
        self.current_user = None
        self.session_token = None
        self.auth_config_id = None
        self.manager = QgsNetworkAccessManager.instance()
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.active = {}  # Keep QNetworkReply objects alive until completion
        self.dialog = None
        self.login_dialog = None
        self.download_reply = None
        self.download_file = None
        self._download_timeout = None
        self.download_target = None
        self.downloading = False
        self._heartbeat_pending = False
        self._closed = False
        self._protected_save_actions = {}
        self._protected_project_active = False
        self._reauth_dialog_pending = False
        self._opening_downloaded_project = False
        self.car_pa_url = "https://geoserverdw.apps.geoapplications.net/geoserver/workspace_fiscalizacao_inteligente/ows"
        self.car_pa_layer = "workspace_fiscalizacao_inteligente:vw_imoveis"

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(self.plugin_dir, 'icon.png')), 'GeoNexus PROvh', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('GeoNexus PROvh', self.action)
        # Reabrir um QGZ GeoNexus nao restaura tokens: pedir login pelo plugin.
        signal = getattr(self.iface, 'projectRead', None)
        if signal is not None:
            signal.connect(self._on_project_read)
        QTimer.singleShot(0, self._on_project_read)

    def unload(self):
        self._closed = True
        signal = getattr(self.iface, 'projectRead', None)
        if signal is not None:
            try:
                signal.disconnect(self._on_project_read)
            except (TypeError, RuntimeError) as exc:
                QgsMessageLog.logMessage(
                    'Nao foi possivel desconectar o sinal de projeto: {}'.format(type(exc).__name__),
                    'GeoNexus PROvh', Qgis.MessageLevel.Warning
                )
        self.heartbeat_timer.stop()
        self.cancel_download()
        self.logout_api()
        self._restore_project_save_actions()
        for reply in list(self.active):
            try:
                reply.abort()
            except RuntimeError as exc:
                QgsMessageLog.logMessage(
                    'Resposta de rede ja indisponivel durante encerramento: {}'.format(type(exc).__name__),
                    'GeoNexus PROvh', Qgis.MessageLevel.Warning
                )
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('GeoNexus PROvh', self.action)

    def run(self):
        if self.session_token:
            self.show_state_selection()
        else:
            self.show_login()

    def _api_request(self, method, endpoint, callback, payload=None, timeout_ms=15000):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise ValueError('Endpoint inválido')
        req = QNetworkRequest(QUrl(API_BASE + endpoint))
        req.setRawHeader(b'Accept', b'application/json')
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        if self.session_token:
            req.setRawHeader(b'Authorization', b'Bearer ' + self.session_token.encode('ascii'))
        if payload is not None:
            req.setRawHeader(b'Content-Type', b'application/json')
            reply = self.manager.post(req, json.dumps(payload).encode('utf-8'))
        else:
            reply = self.manager.get(req)
        self.active[reply] = callback
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(timeout_ms)

        def complete():
            timer.stop()
            cb = self.active.pop(reply, None)
            if cb is None:
                reply.deleteLater()
                return
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = reply.error()
            data = bytes(reply.readAll()).decode('utf-8', errors='replace')
            reply.deleteLater()
            if not self._closed:
                cb(status, error, data)
        reply.finished.connect(complete)
        return reply

    def _install_wms_bearer(self):
        """Attach the session token only to the GeoNexus gateway requests.

        QGIS uses QgsNetworkAccessManager for WMS; do not place credentials in
        QGZ datasource URLs. Fail closed if request preprocessing is unavailable.
        """
        setter = getattr(self.manager, 'setRequestPreprocessor', None)
        if not callable(setter):
            raise RuntimeError('QGIS sem suporte ao pré-processador de requisições WMS.')
        def preprocess(request, *args):
            url = request.url()
            if (self.session_token and url.scheme() == 'https'
                    and url.host().lower() == 'geonexusweb.datacampo.cloud'
                    and url.path() == '/api/v1/qgis/wms'):
                request.setRawHeader(b'Authorization',
                                     b'Bearer ' + self.session_token.encode('ascii'))
        setter(preprocess)
        self._wms_preprocessor = preprocess
        self._wms_preprocessor_installed = True

    def show_login(self, reopen_project=False):
        dlg = QDialog(self.iface.mainWindow())
        self.login_dialog = dlg
        dlg.setWindowTitle('GeoNexus PROvh')
        dlg.setMinimumWidth(430)
        dlg.setStyleSheet(STYLE)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        logo = QLabel()
        pix = QPixmap(os.path.join(self.plugin_dir, 'logo_geonexus.png'))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(240, 95, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)
        title = QLabel('GeoNexus PROvh')
        title.setObjectName('title')
        lay.addWidget(title)
        lay.addWidget(QLabel('Entre com seu e-mail e senha do GeoNexus WEB.'))
        user = QLineEdit()
        user.setPlaceholderText('E-mail')
        user.setText(str(self.settings.value('username', '')))
        password = QLineEdit()
        password.setPlaceholderText('Senha')
        password.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(user)
        lay.addWidget(password)
        remember = QCheckBox('Salvar credenciais com segurança no QGIS')
        lay.addWidget(remember)
        auth = QgsApplication.authManager()
        auth_ready = auth is not None and not auth.isDisabled()
        remember.setEnabled(auth_ready)
        auth_id = str(self.settings.value('geonexus_provh_authcfg', '') or '')
        if auth_ready and auth_id:
            try:
                if auth.masterPasswordIsSet() and auth.setMasterPassword(True):
                    cfg = QgsAuthMethodConfig()
                    if auth.loadAuthenticationConfig(auth_id, cfg, True) and cfg.method() == 'Basic':
                        stored_user = cfg.config('username')
                        stored_password = cfg.config('password')
                        if stored_user and stored_password:
                            user.setText(stored_user)
                            password.setText(stored_password)
                            remember.setChecked(True)
            except (RuntimeError, ValueError, TypeError) as exc:
                QgsMessageLog.logMessage(
                    'Nao foi possivel recuperar credenciais do QGIS Auth Manager: {}'.format(type(exc).__name__),
                    'GeoNexus PROvh', Qgis.MessageLevel.Warning
                )
                remember.setChecked(False)
        status_label = QLabel('')
        status_label.setWordWrap(True)
        status_label.setObjectName('muted')
        lay.addWidget(status_label)
        login_btn = QPushButton('Entrar no GeoNexus PROvh')
        lay.addWidget(login_btn)
        cancel_btn = QPushButton('Cancelar')
        cancel_btn.clicked.connect(dlg.reject)
        lay.addWidget(cancel_btn)

        def persist_credentials(username, pw):
            old_id = str(self.settings.value('geonexus_provh_authcfg', '') or '')
            if not remember.isChecked():
                if old_id and auth_ready and auth.setMasterPassword(True):
                    if not auth.removeAuthenticationConfig(old_id):
                        raise RuntimeError('Não foi possível excluir as credenciais anteriores.')
                self.settings.remove('geonexus_provh_authcfg')
                return
            if not auth_ready or not auth.setMasterPassword(True):
                raise RuntimeError('Desbloqueie o Gerenciador de Autenticação do QGIS.')
            cfg = QgsAuthMethodConfig()
            cfg.setName('GeoNexus PROvh - Acesso GeoNexus WEB')
            cfg.setMethod('Basic')
            cfg.setConfig('username', username)
            cfg.setConfig('password', pw)
            # Create the new configuration before removing the old one.
            if not auth.storeAuthenticationConfig(cfg):
                raise RuntimeError('Não foi possível salvar no QGIS Auth Manager.')
            new_id = cfg.id()
            if not new_id:
                raise RuntimeError('O QGIS não retornou o identificador da configuração.')
            self.settings.setValue('geonexus_provh_authcfg', new_id)
            if old_id and old_id != new_id:
                auth.removeAuthenticationConfig(old_id)

        def attempt():
            username, pw = user.text().strip(), password.text()
            if not username or not pw:
                status_label.setText('Informe o e-mail e a senha.')
                return
            login_btn.setEnabled(False)
            status_label.clear()

            def done(code, error, data):
                if code == 200:
                    try:
                        result = json.loads(data)
                        token = result.get('access_token', '')
                        if not isinstance(token, str) or not token or not token.isascii():
                            raise ValueError('Token inválido')
                        self.session_token = token
                        self._install_wms_bearer()
                    except (ValueError, TypeError, RuntimeError) as exc:
                        self.session_token = None
                        status_label.setText('Não foi possível iniciar a sessão WMS: ' + str(exc))
                        login_btn.setEnabled(True)
                        return
                    try:
                        persist_credentials(username, pw)
                    except Exception as exc:
                        QMessageBox.warning(dlg, 'GeoNexus PROvh',
                            'Login realizado, mas as credenciais não foram atualizadas: ' + str(exc))
                    self.current_user = username
                    self.settings.setValue('username', username)
                    self.settings.remove('password')
                    self.heartbeat_timer.start(30000)
                    if reopen_project:
                        self._refresh_gateway_layers()
                    dlg.accept()
                    return
                if code in (401, 403):
                    status_label.setText('Credenciais inválidas ou assinatura sem acesso.')
                elif code in (301, 302, 303, 307, 308):
                    status_label.setText('Redirecionamento inesperado (HTTP {}).'.format(code))
                else:
                    status_label.setText('Falha de autenticação (HTTP {}).'.format(code or 'sem resposta'))
                login_btn.setEnabled(True)
            self._api_request('POST', '/login', done,
                              payload={'email': username, 'senha': pw})
        login_btn.clicked.connect(attempt)
        password.returnPressed.connect(attempt)
        dlg.exec()
        self.login_dialog = None
        if self.session_token and not self._closed and not reopen_project:
            self.show_state_selection()

    def _project_uses_gateway(self):
        """Detect GeoNexus projects without trusting their filenames or contents."""
        for layer in QgsProject.instance().mapLayers().values():
            if layer.providerType().lower() != 'wms':
                continue
            params = parse_qs(layer.source(), keep_blank_values=True)
            urls = params.get('url', [])
            if len(urls) != 1:
                continue
            parsed = urlparse(unquote(urls[0]))
            if (parsed.scheme == 'https'
                    and parsed.hostname == 'geonexusweb.datacampo.cloud'
                    and parsed.path == '/api/v1/qgis/wms'):
                return True
        return False

    def _on_project_read(self):
        if self._closed or self._opening_downloaded_project or self.session_token:
            return
        if self._reauth_dialog_pending or self.login_dialog is not None:
            return
        if not self._project_uses_gateway():
            return
        self._reauth_dialog_pending = True
        QTimer.singleShot(0, self._prompt_reauthentication)

    def _prompt_reauthentication(self):
        self._reauth_dialog_pending = False
        if self._closed or self.session_token or not self._project_uses_gateway():
            return
        if self.login_dialog is not None:
            return
        self.show_login(reopen_project=True)

    def _refresh_gateway_layers(self):
        for layer in QgsProject.instance().mapLayers().values():
            if layer.providerType().lower() == 'wms':
                layer.triggerRepaint()
        self.iface.mapCanvas().refresh()

    @staticmethod
    def authenticated_wms_uri(uri, auth_id=None):
        """Replace only known GeoServer WMS URLs with the private gateway."""
        fields = uri.split('&')
        url_fields = [field for field in fields if field.split('=', 1)[0].lower() == 'url']
        if len(url_fields) != 1:
            return uri, False
        url = unquote(url_fields[0].partition('=')[2])
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError:
            return uri, False
        legacy_pa = (
            parsed.scheme == 'http' and parsed.hostname == 'srv1185637.hstgr.cloud'
            and port == 8082 and parsed.path.lower() == '/geoserver/assinatura_pa/wms'
            and all(unquote_plus(part.split('=', 1)[1]).strip().upper() in
                    ('MATA CILIAR I', 'MATA CILIAR II')
                    for part in fields if part.split('=', 1)[0].lower() == 'layers')
            and any(part.split('=', 1)[0].lower() == 'layers' for part in fields)
        )
        standard = (
            parsed.scheme == 'http' and parsed.hostname == 'geoservico.com.br'
            and port in (8082, 8086) and parsed.path.startswith('/geoserver/')
        )
        if not (standard or legacy_pa):
            return uri, False
        fields = [field for field in fields if field.split('=', 1)[0].lower()
                  not in ('authcfg', 'username', 'password', 'url')]
        fields.append('url=' + GATEWAY_URL)
        return '&'.join(fields), True

    @staticmethod
    def _safe_xml_document(payload):
        """Parse XML without accepting DTD/entity declarations."""
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError('XML deve ser recebido como bytes.')
        if len(payload) > 32 * 1024 * 1024:
            raise ValueError('XML excede o limite de 32 MiB.')
        if re.search(rb'<!\\s*(?:DOCTYPE|ENTITY)\\b', payload, re.I):
            raise ValueError('DTD e entidades XML nao sao permitidas.')
        document = QDomDocument()
        result = document.setContent(bytes(payload))
        valid = result[0] if isinstance(result, tuple) else bool(result)
        if not valid or document.documentElement().isNull():
            raise ValueError('Documento XML invalido.')
        return document

    def prepare_authenticated_project(self, source, destination, state_uf=None):
        """Change only eligible WMS URLs; never write Bearer tokens to QGZ."""
        if not self.session_token or not self._wms_preprocessor_installed:
            raise ValueError('Sessão WMS não configurada.')
        changed = 0
        with zipfile.ZipFile(source, 'r') as original, zipfile.ZipFile(destination, 'w') as target:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename.lower().endswith('.qgs'):
                    document = self._safe_xml_document(content)
                    layers = document.elementsByTagName('maplayer')
                    for layer_index in range(layers.count()):
                        layer = layers.item(layer_index).toElement()
                        provider = layer.firstChildElement('provider').text().strip().lower()
                        if provider != 'wms':
                            continue
                        node = layer.firstChildElement('datasource')
                        if node.isNull() or not node.text():
                            continue
                        updated, eligible = self.authenticated_wms_uri(node.text())
                        if eligible:
                            if state_uf == 'PA':
                                parts = updated.split('&')
                                for index, part in enumerate(parts):
                                    if part.split('=', 1)[0].lower() == 'layers':
                                        key, sep, value = part.partition('=')
                                        names = unquote_plus(value).split(',')
                                        names = [n if ':' in n else 'forgegeo_pa:' + n for n in names]
                                        parts[index] = key + sep + quote(','.join(names), safe=':,')
                                updated = '&'.join(parts)
                            while not node.firstChild().isNull():
                                node.removeChild(node.firstChild())
                            node.appendChild(document.createTextNode(updated))
                            changed += 1
                    content = document.toByteArray(2).data()
                target.writestr(item, content)
        if not changed:
            raise ValueError('Nenhuma camada WMS elegível foi encontrada no projeto.')
        return changed

    def send_heartbeat(self):
        if not self.session_token or self._heartbeat_pending:
            return
        self._heartbeat_pending = True
        def done(code, error, data):
            self._heartbeat_pending = False
            if code in (401, 403):
                self.logout_api()
                QMessageBox.warning(self.iface.mainWindow(), 'GeoNexus',
                                    'Sessão ou assinatura inválida. Faça login novamente.')
            elif code != 200:
                QgsMessageLog.logMessage('Falha ao validar sessão (HTTP {}).'.format(code),
                                         'GeoNexus', Qgis.MessageLevel.Warning)
        self._api_request('GET', '/me', done, timeout_ms=10000)

    def logout_api(self):
        self.heartbeat_timer.stop()
        self.session_token = None
        self.current_user = None

    def show_state_selection(self):
        if not self.session_token:
            return self.show_login()
        dlg = QDialog(self.iface.mainWindow())
        self.dialog = dlg
        dlg.setWindowTitle('GeoNexus PROvh')
        dlg.resize(640, 670)
        dlg.setStyleSheet(STYLE)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(10)
        title = QLabel('GeoNexus PROvh | Brasil')
        title.setObjectName('title')
        lay.addWidget(title)
        lay.addWidget(QLabel('Selecione o Pará para baixar seu projeto oficial.'))
        search = QLineEdit()
        search.setPlaceholderText('Pesquisar estado ou UF...')
        lay.addWidget(search)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setSpacing(8)
        units = [
            ('AC', 'Acre'), ('AL', 'Alagoas'), ('AP', 'Amapá'),
            ('AM', 'Amazonas'), ('BA', 'Bahia'), ('CE', 'Ceará'),
            ('DF', 'Distrito Federal'), ('ES', 'Espírito Santo'),
            ('GO', 'Goiás'), ('MA', 'Maranhão'), ('MT', 'Mato Grosso'),
            ('MS', 'Mato Grosso do Sul'), ('MG', 'Minas Gerais'),
            ('PA', 'Pará'), ('PB', 'Paraíba'), ('PR', 'Paraná'),
            ('PE', 'Pernambuco'), ('PI', 'Piauí'), ('RJ', 'Rio de Janeiro'),
            ('RN', 'Rio Grande do Norte'), ('RS', 'Rio Grande do Sul'),
            ('RO', 'Rondônia'), ('RR', 'Roraima'), ('SC', 'Santa Catarina'),
            ('SP', 'São Paulo'), ('SE', 'Sergipe'), ('TO', 'Tocantins'),
        ]
        buttons = []
        # Bandeiras das 27 UFs incorporadas ao pacote; sem acesso externo.
        flag_backgrounds = {uf for uf, _ in units}
        download_button = QPushButton('Baixar projeto do Pará')
        download_button.setMinimumHeight(48)
        download_button.setEnabled(False)
        self.selected_state = None
        self.status_label = QLabel('Selecione o Pará para habilitar o download.')
        self.status_label.setObjectName('muted')
        self.status_label.setWordWrap(True)

        def select_para():
            self.selected_state = 'PARÁ'
            download_button.setEnabled(not self.downloading)
            self.status_label.setText('Pará selecionado. Projeto disponível para download.')

        for idx, (uf, name) in enumerate(units):
            # O texto em um QLabel sobreposto nao depende da paleta
            # de QPushButton:disabled (que varia entre temas do QGIS/Windows).
            button = QPushButton()
            button.setMinimumHeight(49)
            button.setObjectName('state_flag_' + uf)
            flag_path = Path(__file__).resolve().parent / 'assets' / ('state_bg_' + uf + '.png')
            flag_css = 'border-image: url("{}");'.format(flag_path.as_posix()) if uf in flag_backgrounds and flag_path.is_file() else ''
            inner = QHBoxLayout(button)
            inner.setContentsMargins(12, 4, 6, 4)
            state_label = QLabel(uf + '  ' + name)
            state_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            state_label.setMinimumHeight(30)
            if uf == 'PA':
                button.setStyleSheet('QPushButton {{ background:#d8f0df; {} border:2px solid #168447; border-radius:7px; }}'.format(flag_css))
                state_label.setStyleSheet('QLabel {{ color:#064A2A; background:rgba(239,250,242,230); border:1px solid #8dc6a1; border-radius:5px; padding:4px 7px; font-size:12px; font-weight:800; }}')
                button.clicked.connect(select_para)
            else:
                button.setEnabled(False)
                button.setStyleSheet('QPushButton:disabled {{ background:#f2f4f6; {} border:1px solid #b7c5d3; border-radius:7px; }}'.format(flag_css))
                state_label.setStyleSheet('QLabel, QLabel:disabled {{ color:#084B78; background:rgba(244,249,253,242); border:1px solid #aac4d8; border-radius:5px; padding:4px 7px; font-size:12px; font-weight:800; }}')
                button.setToolTip('Projeto ainda não habilitado para esta UF.')
            inner.addWidget(state_label)
            grid.addWidget(button, idx // 3, idx % 3)
            buttons.append((uf, name, button))
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

        def filter_states(value):
            term = value.strip().casefold()
            for uf, name, button in buttons:
                button.setVisible(not term or term in uf.casefold() or term in name.casefold())
        search.textChanged.connect(filter_states)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        lay.addWidget(self.progress_bar)
        lay.addWidget(self.status_label)
        download_button.clicked.connect(lambda: self.start_download('PARÁ') if self.selected_state == 'PARÁ' else None)
        lay.addWidget(download_button)
        pa_lookup = QPushButton('Consultar imóvel CAR — Pará')
        pa_lookup.clicked.connect(self.show_car_pa_search)
        lay.addWidget(pa_lookup)
        actions = QHBoxLayout()
        self.cancel_btn = QPushButton('Cancelar download')
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_download)
        actions.addWidget(self.cancel_btn)
        close = QPushButton('Fechar')
        close.clicked.connect(dlg.reject)
        actions.addWidget(close)
        lay.addLayout(actions)
        dlg.finished.connect(lambda _: self.cancel_download())
        dlg.exec()
        self.dialog = None

    def _pa_request(self, params, callback):
        """Consulta OWS assíncrona sem credenciais do GeoServer comercial."""
        url = QUrl(self.car_pa_url + '?' + urlencode(params))
        req = QNetworkRequest(url)
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        reply = self.manager.get(req)
        self.active[reply] = callback
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(20000)

        def finished():
            timer.stop()
            cb = self.active.pop(reply, None)
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            data = bytes(reply.readAll())
            error = reply.error()
            reply.deleteLater()
            if cb and not self._closed:
                cb(status, error, data)
        reply.finished.connect(finished)

    @staticmethod
    def _pa_field_match(fields, candidates):
        def normalized(value):
            return re.sub(r'[^a-z0-9]', '', value.lower())
        for candidate in candidates:
            for field in fields:
                if normalized(field) == normalized(candidate):
                    return field
        return None

    def show_car_pa_search(self):
        """Pesquisa limitada por código CAR ou CPF/CNPJ na vw_imoveis."""
        if not self.session_token:
            return self.show_login()
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle('GeoNexus PROvh | Consulta CAR — Pará')
        dlg.setStyleSheet(STYLE)
        dlg.resize(590, 440)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel('Consultar imóvel CAR — Pará'))
        query = QLineEdit()
        query.setPlaceholderText('Código do imóvel CAR ou CPF/CNPJ')
        layout.addWidget(query)
        search = QPushButton('Buscar imóvel')
        layout.addWidget(search)
        status = QLabel('Informe o código CAR ou CPF/CNPJ.')
        status.setWordWrap(True)
        layout.addWidget(status)
        results = QListWidget()
        layout.addWidget(results)
        load = QPushButton('Carregar imóvel selecionado no QGIS')
        load.setEnabled(False)
        layout.addWidget(load)
        found = []
        schema = {'fields': None}

        def mask_name(value):
            # Exibe apenas a primeira letra de cada palavra, sem revelar o nome completo.
            return ' '.join((part[:1] + '*' * max(3, len(part) - 1)) for part in str(value or '').split()) or 'Nome não informado'

        def mask_document(value):
            digits = re.sub(r'\D', '', str(value or ''))
            if len(digits) == 11:
                return '***.***.***-' + digits[-2:]
            if len(digits) == 14:
                return '**.***.***/****-' + digits[-2:]
            return ''

        def protocol_key(feature):
            value = (feature.get('properties') or {}).get('dat_protocolo')
            if not value:
                return ''
            # Datas ISO 8601 do serviço (UTC) são comparáveis após normalização.
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                return ''

        def done_features(http, error, payload):
            search.setEnabled(True)
            if http != 200:
                status.setText('Falha na consulta WFS (HTTP {}).'.format(http or 'sem resposta'))
                return
            if len(payload) > 15 * 1024 * 1024:
                status.setText('Resposta excessivamente grande. Refine a pesquisa.')
                return
            try:
                obj = json.loads(payload.decode('utf-8'))
                if obj.get('type') != 'FeatureCollection':
                    raise ValueError('Resposta não é GeoJSON')
                features = obj.get('features', [])
                if not isinstance(features, list):
                    raise ValueError('Lista de feições inválida')
                valid = [f for f in features if isinstance(f, dict) and f.get('geometry')]
                # Para cada código CAR, manter somente o protocolo mais recente.
                # Empates preservam o primeiro registro; outros imóveis permanecem na lista.
                newest = {}
                for feature in valid:
                    props = feature.get('properties') or {}
                    car_code = str(props.get('tx_cod_imovel') or '').strip().upper()
                    key = car_code or str(feature.get('id') or id(feature))
                    if key not in newest or protocol_key(feature) > protocol_key(newest[key]):
                        newest[key] = feature
                found[:] = sorted(newest.values(), key=protocol_key, reverse=True)
                results.clear()
                for i, f in enumerate(found):
                    props = f.get('properties') or {}
                    code = next((str(v) for k,v in props.items() if k.lower() in ('tx_cod_imovel','cod_imovel','codigo_imovel','cod_car','codigo_car','numero_car') and v), '')
                    name = next((str(v).strip() for k,v in props.items() if k.lower() == 'tx_nome_imovel' and v), '')
                    municipality = next((str(v).strip() for k,v in props.items() if k.lower() == 'tx_nome_municipio' and v), '')
                    status_value = next((str(v).strip() for k,v in props.items() if k.lower() == 'tx_status_imovel' and v), '')
                    area = next((v for k,v in props.items() if k.lower() == 'num_area_imovel' and v is not None), None)
                    details = [municipality, '{} ha'.format(area) if area is not None else '', status_value]
                    description = ' | '.join(item for item in details if item)
                    label = '{} — {}'.format(i + 1, name or 'Imóvel sem nome informado')
                    if description:
                        label += ' — ' + description
                    doc = next((mask_document(v) for k, v in props.items()
                                if k.lower() in ('tx_cpf_cnpj_proprietario', 'tx_cpf_cadastrante', 'tx_cnpj_cadastrante')
                                and mask_document(v)), '')
                    if doc:
                        label += ' — Documento: ' + doc
                    if code:
                        label += ' — CAR: ' + code
                    results.addItem(label)
                status.setText('{} imóvel(is) com geometria. Selecione para carregar.'.format(len(found)))
                load.setEnabled(bool(found))
            except (ValueError, UnicodeError, TypeError) as exc:
                status.setText('Resposta WFS inválida: {}'.format(exc))

        def query_features(fields):
            value = query.text().strip().upper()
            digits = re.sub(r'\D', '', value)
            if re.fullmatch(r'PA-\d{7}-[A-F0-9]{32}', value):
                matched = [self._pa_field_match(fields, ('tx_cod_imovel', 'cod_imovel', 'codigo_imovel', 'cod_car', 'codigo_car', 'numero_car', 'car'))]
                filter_value = value
            elif len(digits) in (11, 14) and re.fullmatch(r'[\d.\-/ ]+', value):
                # A camada do Para distingue documento do proprietario e do cadastrante.
                candidates = ('tx_cpf_cnpj_proprietario', 'tx_cpf_cadastrante', 'tx_cnpj_cadastrante')
                matched = [self._pa_field_match(fields, (name,)) for name in candidates]
                filter_value = digits
            else:
                search.setEnabled(True)
                status.setText('Informe código CAR do Pará válido ou CPF/CNPJ com 11 ou 14 dígitos.')
                return
            matched = list(dict.fromkeys(field for field in matched if field))
            if not matched:
                search.setEnabled(True)
                status.setText('A camada vw_imoveis não expõe um atributo compatível com este tipo de consulta.')
                return
            # Entradas validadas; nomes dos atributos provêm do DescribeFeatureType.
            cql = ' OR '.join("{}='{}'".format(field, filter_value) for field in matched)
            if len(matched) > 1:
                cql = '(' + cql + ')'
            params = {'service':'WFS','version':'1.0.0','request':'GetFeature',
                      'typeName':self.car_pa_layer,'outputFormat':'application/json',
                      'srsName':'EPSG:4326','maxFeatures':'20','cql_filter':cql}
            status.setText('Consultando imóveis...')
            self._pa_request(params, done_features)

        def done_schema(http, error, payload):
            if http != 200:
                search.setEnabled(True)
                status.setText('Não foi possível consultar o esquema WFS (HTTP {}).'.format(http or 'sem resposta'))
                return
            try:
                document = self._safe_xml_document(payload)
                fields = []
                elements = document.elementsByTagName('*')
                for index in range(elements.count()):
                    element = elements.item(index).toElement()
                    if element.tagName().split(':')[-1] == 'element' and element.hasAttribute('name'):
                        fields.append(element.attribute('name'))
                schema['fields'] = fields
                query_features(fields)
            except Exception as exc:
                search.setEnabled(True)
                status.setText('Falha ao interpretar o esquema WFS: {}'.format(exc))

        def start():
            if not query.text().strip():
                status.setText('Informe o código CAR ou CPF/CNPJ.')
                return
            search.setEnabled(False)
            load.setEnabled(False)
            found.clear()
            results.clear()
            if schema['fields'] is not None:
                query_features(schema['fields'])
            else:
                status.setText('Identificando atributos disponíveis na camada vw_imoveis...')
                self._pa_request({'service':'WFS','version':'1.0.0','request':'DescribeFeatureType',
                                  'typeName':self.car_pa_layer}, done_schema)

        def add_selected():
            idx = results.currentRow()
            if idx < 0 or idx >= len(found):
                status.setText('Selecione um imóvel na lista.')
                return
            feature = dict(found[idx])
            # Não exportar nomes nem documentos pessoais para a camada/GeoJSON.
            private_fields = ('tx_cpf_cnpj_proprietario', 'tx_cpf_cadastrante',
                              'tx_cnpj_cadastrante', 'tx_nome_proprietario',
                              'tx_nome_cadastrante', 'tx_nome_responsavel')
            feature['properties'] = {k: v for k, v in (feature.get('properties') or {}).items()
                                     if k.lower() not in private_fields}
            obj = {'type':'FeatureCollection','features':[feature]}
            # O GeoJSON temporário não inclui nomes pessoais ou documentos conhecidos.
            import tempfile
            path = None
            try:
                fd, path = tempfile.mkstemp(suffix='.geojson', prefix='geonexus_car_pa_')
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    json.dump(obj, out, ensure_ascii=False)
                layer = QgsVectorLayer(path, 'CAR Pará — imóvel consultado', 'ogr')
                if not layer.isValid():
                    raise ValueError('Geometria inválida ou incompatível')
                if layer.featureCount() == 0 or layer.extent().isEmpty():
                    raise ValueError('O imóvel não possui geometria espacial válida para enquadramento')
                # Contorno vermelho, preenchimento totalmente transparente.
                # A simbologia é aplicada apenas à camada CAR carregada nesta consulta.
                if layer.geometryType() == 2:  # QgsWkbTypes.PolygonGeometry
                    symbol = QgsFillSymbol.createSimple({
                        'style': 'no',
                        'outline_style': 'solid',
                        'outline_color': '255,0,0,255',
                        'outline_width': '0.8',
                        'outline_width_unit': 'MM',
                        'joinstyle': 'round',
                    })
                    if symbol is None:
                        raise ValueError('Não foi possível configurar o contorno do imóvel')
                    layer.renderer().setSymbol(symbol)
                project = QgsProject.instance()
                project.addMapLayer(layer)
                canvas = self.iface.mapCanvas()
                # A resposta WFS usa EPSG:4326; o projeto QGIS pode estar em outro SRC.
                # Transformar a extensão evita zoom em local incorreto.
                extent = QgsCoordinateTransform(
                    layer.crs(), canvas.mapSettings().destinationCrs(), project
                ).transformBoundingBox(layer.extent())
                if extent.isEmpty():
                    raise ValueError('Não foi possível calcular a extensão do imóvel')
                # Margem de 8% ao redor do imóvel para enquadramento confortável.
                extent.scale(1.08)
                def zoom_to_property():
                    canvas.setExtent(extent)
                    canvas.refresh()
                status.setText('Imóvel carregado; centralizando o mapa...')
                dlg.accept()
                QTimer.singleShot(0, zoom_to_property)
                # Arquivo temporário é mantido enquanto a camada QGIS o utiliza.
            except Exception as exc:
                status.setText('Falha ao carregar geometria: {}'.format(exc))

        search.clicked.connect(start)
        query.returnPressed.connect(start)
        load.clicked.connect(add_selected)
        dlg.exec()

    def _set_downloading(self, enabled):
        self.downloading = enabled
        if self.dialog:
            self.cancel_btn.setEnabled(enabled)
            self.progress_bar.setVisible(enabled)

    def start_download(self, state):
        if state != 'PARÁ':
            QMessageBox.information(self.dialog, 'GeoNexus PROvh', 'No momento, somente o projeto do Pará está habilitado.')
            return
        if not self.session_token:
            QMessageBox.warning(self.dialog, 'GeoNexus', 'Sessão não autenticada.')
            return
        if self.downloading:
            return
        try:
            url = API_BASE + "/projetos/" + STATE_UF[state]
        except ValueError as exc:
            QMessageBox.critical(self.dialog, 'GeoNexus', str(exc))
            return
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        if not base:
            QMessageBox.critical(self.dialog, 'GeoNexus', 'Diretório local indisponível.')
            return
        folder = Path(base) / 'geonexus' / 'projects'
        folder.mkdir(parents=True, exist_ok=True)
        self.download_target = folder / ('PROJETO_CAR_' + STATE_UF[state] + '.qgz')
        temp = self.download_target.with_suffix('.qgz.part')
        self.download_file = open(str(temp), 'wb')
        self.progress_bar.setValue(0)
        self.status_label.setText('Baixando projeto privado GeoNexus WEB: ' + state)
        self._set_downloading(True)
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b'Authorization', b'Bearer ' + self.session_token.encode('ascii'))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        reply = self.manager.get(request)
        self.download_reply = reply
        timeout = QTimer(reply)
        timeout.setSingleShot(True)
        timeout.timeout.connect(reply.abort)
        timeout.start(120000)

        def write_chunk():
            if self.download_file:
                self.download_file.write(bytes(reply.readAll()))
        def progress(received, total):
            if total > 0:
                self.progress_bar.setValue(min(100, int(received * 100 / total)))
            else:
                self.progress_bar.setFormat('Baixando...')
        def finish():
            timeout.stop()
            write_chunk()
            error = reply.error()
            reply.deleteLater()
            self.download_reply = None
            if self.download_file:
                self.download_file.close()
                self.download_file = None
            self._set_downloading(False)
            http_status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if http_status != 200 or error != QNetworkReply.NetworkError.NoError:
                if http_status in (401, 403):
                    self.logout_api()
                temp.unlink(missing_ok=True)
                self.status_label.setText('Download interrompido ou indisponível.')
                return
            try:
                if temp.stat().st_size > 200 * 1024 * 1024:
                    raise ValueError('Projeto excede o limite de 200 MB')
                with zipfile.ZipFile(temp) as archive:
                    if archive.testzip() is not None or not any(n.endswith('.qgs') for n in archive.namelist()):
                        raise ValueError('O arquivo não é um projeto QGIS válido')
                authenticated = temp.with_suffix('.authenticated.qgz')
                try:
                    count = self.prepare_authenticated_project(temp, authenticated, STATE_UF[state])
                    with zipfile.ZipFile(authenticated) as archive:
                        if archive.testzip() is not None:
                            raise ValueError('Projeto autenticado corrompido')
                    os.replace(str(authenticated), str(self.download_target))
                finally:
                    temp.unlink(missing_ok=True)
                    authenticated.unlink(missing_ok=True)
                self._open_state_project(state, self.download_target)
                QgsMessageLog.logMessage('Projeto com {} camadas WMS autenticadas.'.format(count), 'GeoNexus', Qgis.MessageLevel.Info)
                # Fechar apenas a janela do plugin apos a abertura bem-sucedida.
                # Nao encerrar o complemento, a sessao ou a autenticacao WMS.
                if self.dialog is not None and self.dialog.isVisible():
                    self.dialog.accept()
            except (OSError, zipfile.BadZipFile, ValueError) as exc:
                temp.unlink(missing_ok=True)
                self.status_label.setText('Falha: ' + str(exc))
                return
        reply.readyRead.connect(write_chunk)
        reply.downloadProgress.connect(progress)
        reply.finished.connect(finish)

    def _enable_subscriber_protection(self):
        """Allow QGZ saves; the server enforces access, never the QGIS UI."""
        self._restore_project_save_actions()
        QgsMessageLog.logMessage(
            'Projeto pode ser salvo; acesso WMS requer sessao autorizada no servidor.',
            'GeoNexus', Qgis.MessageLevel.Info
        )

    def _restore_project_save_actions(self):
        for action, originally_enabled in self._protected_save_actions.values():
            try:
                action.setEnabled(originally_enabled)
            except RuntimeError as exc:
                QgsMessageLog.logMessage(
                    'Acao de salvar indisponivel ao restaurar interface: {}'.format(type(exc).__name__),
                    'GeoNexus PROvh', Qgis.MessageLevel.Warning
                )
        self._protected_save_actions.clear()
        self._protected_project_active = False

    def _open_state_project(self, state, project_path):
        """Open the freshly downloaded and authenticated QGZ in QGIS."""
        path = Path(project_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Projeto baixado nao encontrado ou vazio.')
        with zipfile.ZipFile(path, 'r') as archive:
            if archive.testzip() is not None or not any(
                    name.lower().endswith('.qgs') for name in archive.namelist()):
                raise ValueError('Projeto QGIS invalido ou corrompido.')
        project = QgsProject.instance()
        self._opening_downloaded_project = True
        try:
            opened = project.read(str(path))
        finally:
            self._opening_downloaded_project = False
        if not opened:
            raise ValueError('QGIS nao conseguiu abrir o projeto de ' + state)
        self._enable_subscriber_protection()
        canvas = self.iface.mapCanvas()
        canvas.refresh()
        self.status_label.setText('Projeto atualizado carregado (acesso WMS autenticado): ' + state)
        QgsMessageLog.logMessage(
            'Projeto GeoNexus WEB carregado: ' + state,
            'GeoNexus', Qgis.MessageLevel.Info
        )

    def cancel_download(self):
        if self.download_reply is not None:
            self.download_reply.abort()

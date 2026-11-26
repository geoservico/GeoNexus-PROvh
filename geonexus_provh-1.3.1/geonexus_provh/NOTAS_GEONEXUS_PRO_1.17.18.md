GeoNexus PROvh 1.17.18 — Reabertura de projetos salvos

- Reabilita Salvar/Salvar como para projetos GeoNexus (a restricao de interface nao era uma fronteira de seguranca).
- Detecta projetos que usam o gateway WMS GeoNexus ao serem abertos no QGIS.
- Solicita login pelo plugin quando nao existe sessao em memoria, sem forcar novo download.
- Preserva armazenamento opcional no QGIS Auth Manager, sem gravar token no QGZ.
- Atualiza desenho das camadas WMS apos login.
- Preserva login, selecao de UF, download e fechamento automatico da janela.
- A autorizacao continua sendo validada pelo servidor. Um QGZ compartilhado nao fornece autorizacao.
- Testes estaticos realizados; requer teste funcional no QGIS Windows 3.44.

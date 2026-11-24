# GeoNexus PROvh 1.17.15 — Correção de codificação WMS

O projeto PA recebido da API contém duas camadas com espaços no nome: MATA CILIAR I e MATA CILIAR II.
Esta versão aplica percent-encoding aos valores do parâmetro layers ao preparar o QGZ:
`forgegeo_pa:MATA%20CILIAR%20I` e `forgegeo_pa:MATA%20CILIAR%20II`.
O gateway HTTPS e o fluxo de sessão Bearer são preservados.

Validação estática do ZIP e Python concluída; o teste funcional dentro do QGIS ainda é necessário.
Se persistir, verificar resposta GetCapabilities/GetMap e a validade da camada via QgsRasterLayer.
Não remover camadas do projeto.

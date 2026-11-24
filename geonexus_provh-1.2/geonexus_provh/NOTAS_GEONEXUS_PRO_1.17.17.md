# GeoNexus PROvh 1.17.17

Correção de instalação em computadores QGIS sem a biblioteca `defusedxml` instalada globalmente.

A dependência `defusedxml` 0.7.1 é distribuída dentro de `geonexus/_vendor/defusedxml`, sob licença PSF, com o aviso de licença incluído. O plugin usa importação relativa e não altera o ambiente Python do QGIS.

Preservados os fluxos existentes de autenticação, download, fechamento da janela e conversão WMS.

Instalação: Gerenciador de Complementos do QGIS > Instalar a partir de ZIP. Reinicie o QGIS após atualizar.

Validação estática executada; testar no QGIS 3.44.11 do computador de destino.

# GeoNexus PROvh 1.2
- Removidos quatro blocos except/pass silenciosos, inclusive o apontado pelo Bandit na linha 224.
- Falhas de interface e de leitura de credenciais sao registradas sem expor usuario, senha ou token.
- Mantidos identificador geonexus_provh, autenticacao, camadas e interface.
- Validacao estatica realizada; requer teste funcional e nova verificacao no repositorio QGIS.

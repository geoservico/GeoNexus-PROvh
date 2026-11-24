# GeoNexus PROvh 1.1 — correção para análise Bandit
- Removida a cópia vendorizada de defusedxml que disparava alertas na análise de segurança.
- XML de projetos QGIS e DescribeFeatureType processado com QDomDocument do Qt, disponível no QGIS.
- Rejeição de declarações DTD e ENTITY e limite de 32 MiB antes da interpretação.
- Não depende de defusedxml externo; sem mudança de endpoints, credenciais ou permissões.
- Verificações estáticas realizadas; requer teste funcional no QGIS 3.40+ e nova varredura oficial.

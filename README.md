# Trabalho Prático 2

Transferência de arquivos peer-to-peer para a disciplina de Sistemas Distribuídos do Cefet-MG. O enunciado está em [docs/scope.md](docs/scope.md).

Cada peer é um processo que atua como servidor, atendendo pedidos de blocos, e como cliente, pedindo blocos aos vizinhos. O seeder inicial divide o arquivo em blocos de tamanho fixo. Cada leecher se conecta a todos os vizinhos configurados, baixa os blocos, verifica o SHA-256 de cada um e passa a compartilhá-los assim que os recebe. Com todos os blocos, o leecher remonta e verifica o arquivo.

## Requisitos

- Python 3.12 ou superior. Os peers usam somente a biblioteca padrão.
- Docker Engine e Docker Compose v2, para executar as topologias e os estudos de caso.
- matplotlib, para gerar os gráficos da análise:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `p2p/metadata.py` | Fragmentação do arquivo e formato do metadado |
| `p2p/protocol.py` | Empacotamento e desempacotamento das mensagens |
| `p2p/blocks.py` | Registro dos blocos e remontagem do arquivo |
| `p2p/peer.py` | Peer: servidor, cliente, escolha de blocos, logs e resumo |
| `p2p/__main__.py` | Linha de comando (`meta` e `peer`) |
| `compose/` | Topologias de 2 e 4 peers para o Docker Compose |
| `scripts/run_cases.py` | Execução e verificação dos estudos de caso |
| `scripts/analyze.py` | Agregação dos resultados e gráficos |
| `results/` | Resultados dos estudos de caso |

## Execução local

Gere o metadado do arquivo, com blocos de 1024 bytes por padrão:

```
python3 -m p2p meta create arquivo.bin --block-size 1024
```

Inicie o seeder e um leecher em terminais separados:

```
python3 -m p2p peer --host 127.0.0.1 --port 5000 --neighbors 127.0.0.1:5001 --file arquivo.bin --meta arquivo.bin.meta --output-dir seeder
python3 -m p2p peer --host 127.0.0.1 --port 5001 --neighbors 127.0.0.1:5000 --output-dir leecher
```

| Opção | Descrição |
|---|---|
| `--host`, `--port` | Endereço IP e porta do próprio peer |
| `--neighbors` | Lista estática de vizinhos no formato `IP:porta` |
| `--file`, `--meta` | Arquivo completo e seu metadado, usados pelo seeder |
| `--output-dir` | Diretório do arquivo remontado e do `summary.json` |
| `--log-messages` | Registra todas as mensagens trocadas, inclusive `REQUEST`, `PIECE` e `HAVE` |

O leecher obtém o metadado pelo protocolo e, após concluir o download, continua servindo os blocos aos outros peers. `Ctrl+C` ou `SIGTERM` encerra o peer e grava o resumo final.

## Execução com Docker

Cada peer roda em um container com IP fixo na rede `172.28.0.0/24`, na porta 5000:

| Peer | IP | Vizinhos (2 peers) | Vizinhos (4 peers) |
|---|---|---|---|
| peer-a (seeder) | 172.28.0.11 | peer-b | peer-b, peer-c |
| peer-b | 172.28.0.12 | peer-a | peer-a, peer-c, peer-d |
| peer-c | 172.28.0.13 | | peer-a, peer-b, peer-d |
| peer-d | 172.28.0.14 | | peer-b, peer-c |

O diretório `data/peer-x` do host é montado em `/data` no container. Crie os diretórios antes do `up`: se não existirem, o Docker os cria com dono root e os containers não conseguem gravar neles.

```
mkdir -p data/peer-a data/peer-b data/peer-c data/peer-d
head -c 10485760 /dev/urandom > data/peer-a/file.bin
python3 -m p2p meta create data/peer-a/file.bin --block-size 1024
docker compose -f compose/four-peers.yml build
docker compose -f compose/four-peers.yml up -d
docker compose -f compose/four-peers.yml logs -f
docker compose -f compose/four-peers.yml down
```

Para 2 peers, use `compose/two-peers.yml` com `data/peer-a` e `data/peer-b`. A variável `FILE` define o nome do arquivo do seeder (padrão `file.bin`), e os containers rodam com o usuário `HOST_UID:HOST_GID` (padrão `1000:1000`). Após alterar o código, execute o `build` de novo.

## Estudos de caso

```
python3 scripts/run_cases.py
.venv/bin/python scripts/analyze.py
```

`run_cases.py` executa no Docker a matriz completa, com 3 repetições por cenário (`--repetitions`):

- 2 ou 4 peers;
- blocos de 1 KB ou 4 KB;
- arquivos A (10 KB e 20 KB), B (1 MB e 5 MB) e C (10 MB e 20 MB).

Cada execução é aprovada se:

- o arquivo remontado tem o SHA-256 e o tamanho do original;
- todos os blocos vieram de vizinhos configurados;
- os blocos por origem nos logs coincidem com o resumo;
- os containers encerram sem erro.

Os resultados vão para `results/` (`--output`) e substituem os anteriores:

- `results.csv`: uma linha por leecher em cada execução;
- `<cenário>/`: resumo JSON de cada peer e log compactado da execução;
- `summary.md`, `aggregate.csv` e `charts/`: gerados por `analyze.py`.

A análise dos resultados está em [results/summary.md](results/summary.md).

## Protocolo

Cada mensagem tem um cabeçalho de 5 bytes, com o tipo em 1 byte e o tamanho do conteúdo em 4 bytes, seguido do conteúdo. Os inteiros usam ordem big-endian, e os índices de bloco ocupam 4 bytes.

| Tipo | Mensagem | Conteúdo | Uso |
|---|---|---|---|
| 1 | `HELLO` | `IP:porta` do peer | Identifica o peer; quem inicia a conexão envia primeiro e o outro lado responde |
| 2 | `GET_META` | vazio | Pede o metadado |
| 3 | `META` | metadado em JSON | Responde ao `GET_META` |
| 4 | `BITFIELD` | 1 bit por bloco | Informa os blocos que o peer tem; enviado após o `HELLO` |
| 5 | `REQUEST` | índice | Pede um bloco |
| 6 | `PIECE` | índice e dados | Entrega o bloco pedido |
| 7 | `REJECT` | índice | Informa que o peer não tem o bloco pedido |
| 8 | `HAVE` | índice | Anuncia um bloco recebido e verificado |

O leecher pede o metadado a um vizinho que anunciou blocos. Depois, distribui os pedidos entre os vizinhos que têm cada bloco, com até 16 pedidos pendentes por conexão. Um vizinho que não responde em 5 segundos é desconectado, e seus pedidos passam para outros vizinhos.

## Metadado

O arquivo `.meta` é um JSON com os campos:

| Campo | Conteúdo |
|---|---|
| `name` | Nome do arquivo |
| `size` | Tamanho em bytes |
| `block_size` | Tamanho dos blocos em bytes |
| `block_count` | Quantidade de blocos |
| `sha256` | SHA-256 do arquivo completo |
| `blocks` | SHA-256 de cada bloco, na ordem |

## Logs e resumo

Os logs vão para a saída de erro e registram as mensagens de controle, cada bloco recebido com sua origem, o progresso a cada 10% e as falhas. O leecher grava `summary.json` ao concluir o download, e todos os peers o atualizam ao encerrar. O resumo contém tempos, blocos por origem, blocos servidos a cada vizinho, mensagens por tipo e o resultado da verificação do arquivo.

## Decisões de projeto

| Aspecto | Decisão |
|---|---|
| Concorrência | `asyncio`, com sockets não bloqueantes e um laço de eventos por processo, que aceita conexões e se conecta aos vizinhos |
| Vizinhos | Lista estática definida na inicialização, sem tracker |
| Metadado | Só o seeder inicial recebe o `.meta`; os leechers o pedem a um vizinho por vez |
| Armazenamento | Blocos verificados ficam em memória, e o arquivo é gravado uma vez, depois de receber todos e conferir o SHA-256 |
| Escolha de blocos | Aleatória entre os blocos que faltam e que o vizinho tem, para que os leechers tenham blocos diferentes a trocar |
| Pedidos simultâneos | Até 16 por conexão; em medições com 20 MB, a vazão praticamente se estabiliza a partir desse valor |
| Falhas | Bloco rejeitado ou com hash inválido é pedido a outro vizinho; vizinho sem resposta em 5 s é desconectado; conexões de saída são refeitas a cada 1 s |
| Conexões duplicadas | Quando dois peers se conectam um ao outro, os dois mantêm a conexão iniciada pelo peer de menor `IP:porta` |
| Encerramento | `SIGTERM` e `SIGINT` fecham as conexões e gravam o resumo, preservando as estatísticas ao parar os containers |

## Resultados

Os 144 downloads dos estudos de caso foram aprovados em todas as verificações. Com o arquivo de 20 MB:

| Peers | Bloco | Tempo do último leecher (s) | Vazão por leecher (MB/s) | Mensagens por bloco |
|---|---|---|---|---|
| 2 | 1 KB | 1,775 ± 0,085 | 11,8 ± 0,6 | 3,00 |
| 2 | 4 KB | 0,544 ± 0,029 | 40,3 ± 0,5 | 3,00 |
| 4 | 1 KB | 3,559 ± 0,032 | 5,9 ± 0,1 | 7,99 |
| 4 | 4 KB | 1,092 ± 0,054 | 19,9 ± 1,0 | 7,97 |

Com blocos de 4 KB, a vazão é cerca de 3,4 vezes maior que com 1 KB. O arquivo passa a ter 4 vezes menos blocos e, na mesma proporção, menos mensagens e verificações de hash.

Com 4 peers, cada leecher fica cerca de 2 vezes mais lento, mas o sistema entrega 3 cópias do arquivo. Com blocos de 1 KB, a vazão agregada (total entregue dividido pelo tempo do último leecher) sobe de 11,3 MB/s para 16,9 MB/s.

Com 4 peers e arquivos a partir de 5 MB, o seeder enviou de 34% a 37% de todos os blocos transferidos, o equivalente a cerca de uma cópia do arquivo. Os leechers enviaram o restante, e peer-d, que não é vizinho do seeder, recebeu todos os blocos de peer-b e peer-c.

A tabela completa e os gráficos estão em [results/summary.md](results/summary.md).

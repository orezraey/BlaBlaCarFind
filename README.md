# BlaBlaCarFind

Bot de Telegram que monitora uma rota do BlaBlaCar e avisa assim que uma carona nova
é publicada.

Caronas boas somem rápido: quem procura uma viagem específica acaba abrindo o site
várias vezes por dia para ver se alguém publicou algo. O bot faz essa checagem
sozinho e manda a carona no Telegram assim que ela aparece — junto com os dados que
ajudam a decidir se vale reservar, incluindo com que frequência aquele motorista
cancela caronas.

## Recursos

- Monitora várias rotas ao mesmo tempo, cada uma com origem, destino e data próprios.
- Notifica apenas caronas **novas**: as que já existiam quando a rota foi cadastrada
  não geram alerta.
- Mostra o histórico de cancelamento do motorista, a nota, o selo SuperDriver e se o
  perfil é verificado.
- Avisa quando a reserva depende de aprovação manual do motorista.
- Encerra o monitoramento automaticamente quando a data da viagem passa.

## Requisitos

- Python 3.10 ou superior
- Um bot do Telegram criado pelo [@BotFather](https://t.me/BotFather)

## Instalação

```bash
git clone https://github.com/orezraey/BlaBlaCarFind.git
```

```bash
cd BlaBlaCarFind && pip install -r requirements.txt
```

Crie o arquivo de configuração a partir do modelo:

```bash
copy .env.example .env
```

Em Linux ou macOS, use `cp .env.example .env`.

Abra o `.env` e informe o token recebido do @BotFather:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
```

Inicie o bot:

```bash
python bot.py
```

Ao iniciar, o terminal exibe `bot no ar; ciclo a cada 15 min`. A partir daí, basta
enviar `/start` para o bot no Telegram.

## Configuração

As opções são lidas do arquivo `.env`. Variáveis de ambiente têm precedência sobre o
arquivo, o que permite usar systemd, Docker ou CI sem alterar nada no projeto.

| Variável | Padrão | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Obrigatória.** Token do bot |
| `POLL_MINUTES` | `15` | Intervalo entre as checagens, em minutos |
| `BLABLACARFIND_DB` | `blablacarfind.db` | Caminho do banco SQLite |
| `TIMEZONE` | fuso do sistema | Fuso usado para determinar a data atual (nome IANA) |

O bot detecta o fuso horário do sistema automaticamente e o exibe ao iniciar. Defina
`TIMEZONE` apenas quando o servidor estiver em um fuso diferente do das viagens — um
VPS configurado em UTC, por exemplo, consideraria as 21h de Brasília como o dia
seguinte, adiantando as datas oferecidas no cadastro.

```ini
TIMEZONE=America/Sao_Paulo
```

## Uso

| Comando | Descrição |
|---|---|
| `/start` | Apresenta o bot e lista os comandos |
| `/monitorar` | Cadastra uma rota |
| `/rotas` | Lista as rotas monitoradas |
| `/checar` | Consulta as rotas imediatamente |
| `/parar [nº]` | Encerra o monitoramento de uma rota |

O comando `/monitorar` conduz o cadastro em três etapas: cidade de origem, cidade de
destino e data. Nas duas primeiras, o bot apresenta uma lista de lugares para escolha,
evitando ambiguidade entre cidades de nome parecido. A data pode ser escolhida entre
os próximos sete dias ou digitada no formato `DD/MM/AAAA`.

Ao concluir o cadastro, o bot registra as caronas já publicadas e informa quantas
encontrou. Essas não geram notificação — o objetivo é avisar apenas sobre o que
aparecer a partir daquele momento.

## Exemplo de notificação

```
🚗 Nova carona encontrada
Campinas - SP → São Paulo, SP · qui 20/ago

🕐 05:20 → 06:30  (1h10)
📍 Distrito Industrial → São Paulo
💰 R$ 34,00

👤 Daniel
⭐ 4,77 · ✅ verificado
🟡 Cancela caronas às vezes
🚙 VOLKSWAGEN POLO - Cinza
⚠️ A reserva só vale depois que o motorista aprovar

💬 Pouco espaço para mala, consultar antes
                                    [Ver no BlaBlaCar]
```

O indicador de cancelamento reflete o histórico do motorista no BlaBlaCar:

| Indicador | Significado |
|---|---|
| 🟢 | Nunca ou raramente cancela |
| 🟡 | Cancela caronas às vezes |
| 🔴 | Cancela com frequência |
| ⚪ | Ainda não possui histórico |

Todas as caronas novas são notificadas, sem filtro. O indicador serve para informar a
decisão, não para esconder opções.

## Escolhendo o intervalo de checagem

O volume de consultas depende de quantas caronas a rota tem, já que os resultados são
paginados de dez em dez.

| Rota | Caronas | Consultas por checagem | Por dia, a cada 15 min |
|---|---|---|---|
| São Paulo → Rio de Janeiro | ~3 | 1 | ~96 |
| São Paulo → Belo Horizonte | ~5 | 1 | ~96 |
| Campinas → São Paulo | ~103 | ~11 | ~1.056 |

Rotas metropolitanas concentram muito mais caronas do que trechos intermunicipais
longos. Para monitorar várias rotas desse tipo, um valor maior de `POLL_MINUTES`
reduz o volume proporcionalmente.

## Como funciona

O bot consulta a API que o site do BlaBlaCar utiliza e compara o resultado com o que
já foi visto naquela rota, notificando a diferença. Para cada carona nova, faz uma
segunda consulta que traz os dados do motorista — eles não estão disponíveis na
listagem de resultados.

| Arquivo | Responsabilidade |
|---|---|
| `bot.py` | Comandos do Telegram e ciclo de monitoramento |
| `blablacar.py` | Cliente da API: busca, detalhe da carona e geocodificação |
| `storage.py` | Banco SQLite com as rotas e as viagens já notificadas |
| `clock.py` | Resolução do fuso horário e data de referência |
| `API.md` | Documentação da API utilizada |

O projeto roda como um processo único e não exige serviços externos além do Telegram.

## Solução de problemas

**`A sintaxe do nome do arquivo... está incorreta`** — a sintaxe `$env:VARIAVEL` é do
PowerShell e não funciona no Prompt de Comando. Com a configuração pelo `.env` isso
deixa de ser necessário: basta executar `python bot.py`.

**`TELEGRAM_BOT_TOKEN não definido`** — o arquivo `.env` não existe ou está sem o
token. Confirme que ele está na mesma pasta de `bot.py` e que a linha do token não
ficou em branco.

**O bot não responde no Telegram** — verifique se o processo continua em execução no
terminal. O bot funciona por consulta ativa (polling) e precisa permanecer rodando.

**`conexão com o Telegram falhou (NetworkError); reconectando automaticamente`** —
oscilação de rede na conexão com a API do Telegram. O cliente refaz a conexão sozinho,
com espera progressiva, e o monitoramento continua. Só merece atenção se as linhas se
repetirem de forma contínua, o que indica problema de conectividade no servidor.

**`outra instância do bot está usando o mesmo token`** — o Telegram permite apenas uma
conexão de polling por token. Encerre a instância duplicada; a que restar volta a
funcionar sozinha.

**Nenhuma carona é encontrada** — nem toda rota tem caronas publicadas para a data
escolhida. Confirme no site do BlaBlaCar; datas muito distantes costumam estar vazias.

## Limitações

- Monitora apenas caronas entre particulares. Passagens de ônibus vendidas pela
  plataforma não são acompanhadas, por não terem motorista nem histórico de
  cancelamento.
- Depende da API interna do BlaBlaCar, que não é pública nem versionada e pode mudar
  sem aviso.
- O acesso exige emulação da assinatura TLS do Chrome, feita pela biblioteca
  `curl_cffi`.

## Aviso

Projeto sem qualquer vínculo com o BlaBlaCar, desenvolvido para uso pessoal e fins
educacionais. Utiliza uma API interna, não oficial e sem suporte. O uso é de
responsabilidade de quem executa o projeto.

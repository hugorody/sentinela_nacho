# Memória do projeto — Sentinela Nacho

Atualizado em: 12 de julho de 2026.

Este documento registra o ponto atual do trabalho para permitir uma retomada
sem depender do histórico da conversa. Consulte também o `README.md` para uso e
instalação.

## Estado atual

O projeto é um dashboard Flask local, executado por padrão na porta 8001. Ele
integra câmeras RTSP, OpenCV, gravações, eventos faciais, alarmes por e-mail,
Tuya, cenas e descoberta da rede interna.

Existem alterações locais ainda não commitadas. Antes de qualquer trabalho
futuro, executar `git status --short` e preservar essas mudanças.

## Inventário da rede

Foi criado `network_monitor.py`, com persistência em SQLite no arquivo local
`network_inventory.db`.

Decisões implementadas:

- Ciclo automático leve de ping/ARP a cada 5 minutos.
- Três ausências consecutivas para confirmar que um dispositivo saiu.
- Eventos `new`, `online` e `offline` somente nas transições.
- Primeira varredura cria a base sem gerar uma tempestade de eventos.
- A aba lê o cache; a leitura visual é renovada a cada 15 segundos.
- `Atualizar agora` executa ping/ARP.
- `Análise completa` acrescenta portas conhecidas, mDNS e SSDP.
- Nome personalizado, marcação de conhecido, primeira e última aparição.
- Edição do nome inline, igual à edição das câmeras.

Caso observado: `iPhone de Hugo`, MAC `42:5e:ef:dc:f5:8d`, IP observado
`192.168.15.4`. A saída do teste foi registrada em 12/07/2026 às 18:31:26. O
problema percebido era a falta de atualização automática da interface, já
corrigida.

## Reconhecimento facial

Pipeline atual:

- Detecção: YuNet `face_detection_yunet_2023mar.onnx`.
- Embeddings: SFace `face_recognition_sface_2021dec.onnx`.
- CPU disponível: Ryzen 7 5800H, 8 núcleos/16 threads.
- A GPU NVIDIA não estava disponível pelo driver no momento da auditoria.
- Intervalo facial padrão: 0,7 segundo por câmera.
- Largura de detecção configurada pelo Engine: 960 px.

Diagnóstico realizado sobre os dados locais:

- 450 registros no histórico na data da auditoria.
- A maior parte dos rostos tinha aproximadamente 75–125 px.
- O cadastro anterior tinha 74 embeddings: Hugo 33, Arya 24 e Yhasmin 17.
- Teste leave-one-out simples: 60/74 amostras encontravam primeiro a identidade
  correta, evidenciando contaminação e sobreposição.
- Foram encontradas imagens borradas, de costas, parcialmente cortadas e sem um
  rosto humano útil.
- Arya é um cachorro; YuNet/SFace são um pipeline de rosto humano.

Melhorias já implementadas:

- `quality_check()` verifica tamanho, confiança, nitidez por Laplaciano,
  exposição, inclinação e geometria dos landmarks.
- Capturas ruins não entram no histórico utilizável nem no cadastro.
- O treinamento remove outliers internos por identidade.
- São mantidas no máximo 15 amostras representativas por pessoa.
- A comparação usa a média das três melhores amostras de cada pessoa, em vez do
  máximo global entre dezenas de embeddings.
- A identidade precisa superar o limiar e abrir margem de 0,06 para o segundo
  candidato.
- O mesmo resultado precisa aparecer em dois quadros consecutivos, associado
  por sobreposição da caixa facial.
- `historico_faces/non_human.json` exclui identidades não humanas do cadastro
  sem apagar seus rótulos ou imagens. Arya está listada nesse arquivo.

O banco ativo foi reconstruído após as melhorias:

- Hugo: 15 amostras.
- Yhasmin: 14 amostras.
- Total: 29 embeddings humanos.

## Próximos passos recomendados

1. Reiniciar o dashboard e observar falsos positivos e falsos negativos reais
   por alguns dias.
2. Registrar exemplos de erro com câmera, horário, identidade prevista e score.
3. Ajustar limiar, margem, nitidez e pose usando esses erros reais.
4. Adicionar uma segunda análise na resolução original apenas na região facial,
   mantendo a detecção geral reduzida para economizar CPU.
5. Se SFace continuar insuficiente, testar ArcFace/InsightFace lado a lado, sem
   substituir imediatamente o pipeline atual. Considerar consumo em CPU e a
   licença dos modelos distribuídos pelo InsightFace.
6. Para reconhecer Arya, criar uma funcionalidade separada com detector e
   embedding apropriados para animais; não recolocá-la no banco humano.

## Nacho — assistente por voz

O trabalho foi iniciado no branch `feature/nacho-voice-ui`. A decisão é manter
o Nacho como serviço separado do painel administrativo do Sentinela.

Primeira entrega implementada:

- `nacho_app.py` executa em `0.0.0.0:8002` por padrão.
- `templates/nacho.html` contém uma única ação: a tampa central.
- `static/nacho.css` desenha uma tampa metálica original, responsiva, sem usar
  ou copiar o arquivo de referência fornecido.
- `static/nacho.js` cria a serrilha, solicita o microfone e anima a interface
  conforme o volume captado.
- Estados visuais prontos: `idle`, `listening`, `thinking`, `speaking`, `error`.
- `window.NachoUI.setState()` é o ponto central de controle da animação.
- `/api/health` informa separadamente se voz e Sentinela estão conectados.
- Sem chave, nenhum áudio é enviado e a interface funciona em modo de teste.

Integração de voz e consultas implementada na etapa seguinte:

- WebRTC pela interface unificada `/v1/realtime/calls` da OpenAI.
- A chave `openai_api_key` é lida apenas no backend; o navegador envia ao Nacho
  somente a oferta SDP e recebe a resposta SDP.
- Modelo configurável por `nacho_realtime_model`, padrão `gpt-realtime-2.1`.
- Voz configurável por `nacho_voice`, padrão `marin`.
- PIN opcional `nacho_pin` protegido com HTTP Basic; não adiciona controles à
  interface. Deve ser configurado antes de acesso por outros dispositivos.
- `nacho_tools.py` contém a allowlist de ferramentas somente-leitura.
- Ferramentas: status do sistema, presença nas câmeras, eventos de câmera,
  lista smart, estado de luz/sensor e eventos da rede.
- O evento Realtime `response.output_item.done` executa a função local e devolve
  `function_call_output`; funções fora da allowlist recebem 404.
- O Sentinela expõe `/api/nacho/cameras`, sem imagens ou URLs RTSP.
- Correção de 12/07/2026: campos vazios de `nacho_realtime_model` e
  `nacho_voice` no `.env` precisam cair nos padrões com `value or default`.
  Antes da correção, a OpenAI respondia `missing_model` e `invalid_value` para
  voz vazia, exibidos na interface como “A OpenAI recusou a sessão de voz”.
- O fluxo corrigido foi validado de ponta a ponta em Chrome headless com mídia
  simulada e chegou ao estado `listening` / “Pode falar, estou ouvindo”.
- Correção adicional para dispositivos da rede: após `setLocalDescription`, o
  cliente aguarda o término de `iceGatheringState` e envia
  `peer.localDescription.sdp`, já com os candidatos ICE. Antes, enviar
  imediatamente `offer.sdp` podia funcionar em localhost e ficar preso em
  “Conectando a conversa segura” em celular/tablet. O canal agora também tem
  timeout de 15 segundos e mensagens distintas por etapa.
- Após um timeout ainda observado em outro navegador, o cliente passou a exibir
  no erro os estados ICE/conexão e os tipos de candidatos locais/remotos, além
  de registrar `icecandidateerror` no console. Usar esses dados antes de optar
  por fallback WebSocket ou voz por turnos HTTP.
- Diagnóstico recebido: `ICE connected`, `connection connected`, candidatos
  local/remoto `host`, mas o RTCDataChannel não abriu. Como a mídia WebRTC está
  válida, o cliente agora permite voz em modo degradado, controla o estado
  `speaking` pelos eventos do elemento `<audio>` e informa que as consultas do
  Sentinela estão temporariamente indisponíveis. Só falha se mídia e dados não
  conectarem.
- Como houve também `connection failed` em localhost, foi adicionado fallback
  automático por turnos HTTP em `nacho_turn.py`: MediaRecorder envia WebM ao
  backend, `gpt-4o-mini-transcribe` transcreve, `gpt-5.6-sol` responde com o
  mesmo loop de ferramentas e `gpt-4o-mini-tts` gera MP3. O fallback foi
  validado com chamadas reais mínimas de Responses, TTS e transcrição.
- Decisão posterior: HTTP por turnos tornou-se o transporte padrão, não apenas
  fallback. Fluxo: navegador -> Nacho HTTP -> Sentinela HTTP/OpenAI HTTPS ->
  navegador. `nacho_voice_transport=realtime` reativa WebRTC experimental; valor
  ausente ou `http` usa o caminho recomendado.
- Comportamento conversacional: um toque inicia a sessão; VAD leve no navegador
  detecta fala e envia o turno após cerca de 1,15 s de silêncio. A IA continua
  na nuvem: o VAD só delimita a gravação. Depois da resposta, o microfone reabre
  automaticamente. O segundo toque encerra toda a conversa. O frontend mantém o
  `response_id` e o backend envia `previous_response_id` à Responses API para
  preservar referências entre turnos. Encerrar aborta requisições pendentes.
- Ajuste do VAD: um pico isolado do microfone não basta; exige 180 ms de nível
  sustentado e pelo menos 350 ms de fala confirmada antes de enviar após 1,15 s
  de silêncio. Falhas agora distinguem transcrição, resposta e TTS e a conversa
  volta a ouvir automaticamente. A cadeia completa foi validada com áudio
  sintético real nas três APIs.
- Falha após a primeira interação: dois turnos com `previous_response_id` foram
  validados diretamente e preservaram contexto. A hipótese restante era eco ou
  auto gain na reabertura do microfone após o TTS. O VAD agora ignora os
  primeiros 650 ms de cada gravação e espera 850 ms após o fim da resposta antes
  de reabrir o microfone.

Próxima etapa do Nacho:

1. Configurar HTTPS e um nome local, idealmente `https://nacho.local`.
2. Configurar `nacho_pin` e, depois, evoluir para cadastro/revogação individual
   dos dispositivos domésticos.
3. Configurar a chave OpenAI pelo painel e testar uma sessão real.
4. Adicionar transcrição discreta e telemetria local de erros/custos.
5. Expandir consultas para alarmes e explicações de cenas.
6. Só depois adicionar ações, com confirmação obrigatória para operações
   sensíveis.

### HTTPS doméstico

- `https_setup.py` gera uma CA doméstica RSA 3072 e certificado de servidor RSA
  2048, com SAN para IP LAN, `127.0.0.1`, `localhost`, `nacho.local` e hostname.
- Certificados atuais gerados para `192.168.15.10` em `certs/`, ignorado pelo
  Git; chaves privadas usam permissão `0600`.
- `nacho_app.py --https` usa `certs/nacho-server.crt` e `.key`.
- O Sentinela publica somente a CA pública em `/nacho-ca.crt` para instalação.
- No iPhone, instalar o perfil não basta: ativar confiança total em Ajustes >
  Geral > Sobre > Ajustes de Certificados Confiáveis.
- Se o IP mudar, regenerar certificado e reinstalar a CA.

## Validações já executadas

- `python3 -m py_compile` nos módulos Python alterados.
- `node --check static/app.js`.
- `git diff --check`.
- Teste dos endpoints de rede com cliente Flask.
- Teste das transições online/offline do inventário.
- Teste de identificação direta no banco limpo.
- Teste da confirmação temporal: primeiro quadro pendente, segundo confirmado.

## Arquivos centrais

- `app.py`: API Flask e integração dos módulos.
- `nacho_app.py`: serviço independente e futura orquestração do assistente.
- `nacho_tools.py`: contratos e execução das consultas autorizadas.
- `engine.py`: streams, gravação e ciclo facial.
- `face_recog.py`: detecção, qualidade, embeddings, cadastro e agrupamento.
- `network_monitor.py`: inventário persistente e atividade da rede.
- `netscan.py`: ping, ARP, portas, mDNS e SSDP.
- `alarms.py`: alarmes de câmera e dispositivos smart.
- `scenes.py`: automações.
- `static/app.js`, `static/style.css`, `templates/index.html`: painel web.
- `static/nacho.js`, `static/nacho.css`, `templates/nacho.html`: interface Nacho.

## Cuidados para a retomada

- Não versionar credenciais, vídeos, histórico facial ou o banco da rede.
- Não apagar rótulos ou imagens ao limpar o cadastro; reconstruir apenas
  `known_faces.json`.
- Não usar o limiar oficial do SFace como verdade universal: calibrar com as
  imagens reais das câmeras.
- Não misturar reconhecimento humano e de animais.
- Mudanças no pipeline facial exigem reconstruir o cadastro e reiniciar o
  dashboard ou recarregar explicitamente `KnownFaces`.

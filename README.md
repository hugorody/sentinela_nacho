# Sentinela Nacho

Dashboard local de monitoramento com câmeras RTSP, detecção e reconhecimento
facial, gravação, alarmes, automações Tuya e inventário da rede interna.

O estado técnico consolidado e os próximos passos estão em
[`PROJECT_MEMORY.md`](PROJECT_MEMORY.md).

## Instalação

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Modelos ONNX

Os modelos nao sao versionados no git. Baixe do
[opencv_zoo](https://github.com/opencv/opencv_zoo) e coloque em `models/`:

- `face_detection_yunet_2023mar.onnx` ([face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet))
- `face_recognition_sface_2021dec.onnx` ([face_recognition_sface](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface))

## Configuração das câmeras

As credenciais das cameras ficam fora do git:

- copie `cameras.json.example` para `cameras.json` e preencha usuario/senha,
  ou rode `python3 discover.py` para descobrir as cameras na rede.

## Execução

```bash
python3 app.py                       # http://127.0.0.1:8001
python3 app.py --host 0.0.0.0        # acesso pela rede local
python3 app.py --start               # inicia também os streams
```

O Nacho roda como serviço separado:

```bash
python3 nacho_app.py                  # http://0.0.0.0:8002
```

### HTTPS para iPhone e iPad

O microfone no Safari exige uma origem HTTPS confiável. Para a rede atual:

```bash
python3 https_setup.py --ip 192.168.15.10   # executar uma vez
python3 app.py                               # Sentinela :8001
python3 nacho_app.py --https                 # Nacho HTTPS :8002
```

No iPhone conectado ao mesmo Wi-Fi:

1. Abra `http://192.168.15.10:8001/nacho-ca.crt` no Safari.
2. Permita o download do perfil.
3. Em **Ajustes > Geral > VPN e Gerenciamento de Dispositivo**, instale o perfil
   `Sentinela Nacho Home CA`.
4. Em **Ajustes > Geral > Sobre > Ajustes de Certificados Confiáveis**, ative
   confiança total para `Sentinela Nacho Home CA`.
5. Abra `https://192.168.15.10:8002` e permita o microfone.

Se o IP do servidor mudar, gere novamente os certificados com o novo `--ip` e
reinstale a CA nos dispositivos. Nunca compartilhe `certs/nacho-ca.key`.

A tampa central inicia uma conversa no modo HTTP por turnos. Fale naturalmente:
uma pausa de aproximadamente 1,2 segundo envia a frase, a resposta é falada e o
microfone volta a ouvir automaticamente. Um novo toque encerra a conversa. O
contexto é preservado entre os turnos. O Nacho local chama a OpenAI por HTTPS e
consulta o Sentinela por HTTP quando necessário. Sem `openai_api_key`,
funciona apenas como teste visual do microfone. Os estados são `idle`,
`listening`, `thinking`, `speaking` e `error`. Em celulares e tablets, o
microfone exigirá HTTPS local.

Configuração inicial recomendada em **Configurações** do Sentinela:

- OpenAI API Key.
- Modelo Realtime e voz, ou os padrões do projeto.
- PIN de acesso do Nacho antes de liberá-lo na rede doméstica.

As primeiras ferramentas são somente-leitura: status, presença nas câmeras,
eventos, dispositivos smart, estado de sensores/luzes e atividade da rede.

WebRTC permanece disponível como transporte experimental em Configurações. O
modo recomendado não depende de ICE/UDP: o servidor usa as APIs de transcrição,
Responses e síntese de voz, mantendo a IA integralmente na nuvem e as mesmas
ferramentas somente-leitura disponíveis.

## Recursos

- Câmeras RTSP com reconexão automática e visualização MJPEG.
- Gravação contínua em segmentos MP4.
- Detecção, cadastro e reconhecimento facial.
- Histórico de eventos com miniaturas.
- Alarmes por câmera e sensores smart, com horários e e-mail.
- Dispositivos Tuya, cenas e automações.
- Descoberta e inventário persistente da rede interna.
- Interface independente do Nacho, preparada para voz e acesso mobile.

## Gravação

Cada camera tem um botao **REC** no painel que liga/desliga a gravacao
continua. Os videos sao salvos em segmentos `.mp4` (10 min por padrao) na
pasta `gravacoes/`, que fica fora do git. Para gravar tudo desde o inicio,
instancie o `Engine` com `record_all=True`.

## Inventário da rede

A aba **Minha rede** mantem um inventario local em `network_inventory.db`.
O monitor automático faz somente ping/ARP a cada 5 minutos e registra entrada
ou saída apenas depois de 3 varreduras ausentes. Abrir a aba e sua atualização
visual a cada 15 segundos leem somente o cache. **Atualizar agora** executa uma
leitura leve; **Análise completa** executa, sob demanda, a descoberta mais
detalhada de portas, mDNS e SSDP.

O nome é editado diretamente na tabela, com o mesmo comportamento usado nas
câmeras: Enter confirma, Escape cancela e sair do campo salva. A marcação de
dispositivo conhecido também é persistida. O banco é local e não é versionado.

## Reconhecimento facial

O pipeline usa YuNet para detecção e SFace para embeddings. Antes de cadastrar
ou reconhecer, valida tamanho, nitidez, exposição e pose do rosto. O treinamento
remove amostras inconsistentes, limita cada pessoa a 15 exemplos representativos
e a identificação exige concordância entre
as melhores amostras, margem para o segundo candidato e dois quadros seguidos.

Nomes presentes em `historico_faces/non_human.json` continuam visíveis no
histórico, mas não entram no reconhecedor humano. Atualmente `Arya` está nessa
lista porque reconhecimento de animais exige um modelo separado.

Para reconstruir o cadastro depois de revisar os nomes:

```bash
python3 face_recog.py enroll --log ./historico_faces
```

## Dados locais não versionados

- `.env`: credenciais e segredos.
- `cameras.json`: URLs e credenciais RTSP.
- `historico_faces/`: imagens, rótulos e embeddings.
- `network_inventory.db`: inventário e atividade da rede.
- `gravacoes/`: segmentos de vídeo.
- `alarms.json`, `scenes.json` e arquivos Tuya: configurações locais.

## Verificações rápidas

```bash
python3 -m py_compile app.py engine.py face_recog.py network_monitor.py nacho_app.py nacho_tools.py
node --check static/app.js && node --check static/nacho.js
git diff --check
```

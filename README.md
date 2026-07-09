# Sentinela Nacho

Dashboard de monitoramento com cameras RTSP (Intelbras Mibo), com deteccao e
reconhecimento facial via OpenCV.

## Instalacao

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

## Configuracao das cameras

As credenciais das cameras ficam fora do git:

- copie `cameras.json.example` para `cameras.json` e preencha usuario/senha,
  ou rode `python3 discover.py` para descobrir as cameras na rede.

## Uso

```bash
python3 app.py            # dashboard web em http://localhost:5000
```

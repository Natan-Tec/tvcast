# TV Cast

O **PC** é o servidor e o controlador. A **TV** (APK) só mostra o que você escolher no PC:
tela inteira, uma janela, um vídeo ou uma imagem.

```
PC  (server.py + painel em http://localhost:8080)  ──Wi-Fi/cabo──▶  TV (APK abre http://IP-do-PC:8080/tv)
```

## 1. No PC

1. Instale o Python 3.9+ (no instalador marque "Add Python to PATH").
2. Dê dois cliques em `run.bat` (Linux: `./run.sh`). Na primeira vez ele instala as dependências.
3. O painel abre sozinho em `http://localhost:8080` e mostra "Na TV digite: 192.168.x.x:8080".
4. Se o Firewall do Windows perguntar, permita em **redes privadas**.

## 2. Gerar o APK da TV

Igual ao que você já faz com o Reaper Remote:

1. Coloque esta pasta num repositório do GitHub (o arquivo `.github/workflows/build-apk.yml` precisa ficar na raiz do repositório).
2. Aba **Actions** → "Build APK" → Run workflow.
3. Baixe o artefato `TVCast-apk`, instale na TV (é um APK universal, funciona em TV 32 bits).

## 3. Usar

1. Abra o **TV Cast** na TV e digite o IP mostrado no painel. Ele lembra o IP e reconecta sozinho nas próximas vezes.
2. No painel do PC escolha: **Tela inteira**, **Janela**, **Vídeo** ou **Imagem**. Aparece na TV na hora.
3. Botão **Parar projeção** volta a TV para a tela de espera.
4. Controle remoto da TV: **VOLTAR** na projeção volta para a tela de IP.

## Limitações

- **Tela e janela vão sem áudio** (é um fluxo de imagens). Vídeo de arquivo toca com áudio.
- **Janela** = região da tela onde ela está. O app a traz para a frente; se outra janela cobrir, a TV mostra a que estiver por cima.
- **Vídeo**: a TV toca o que o WebView dela suporta (MP4 H.264, WebM). MKV/AVI podem falhar; a TV avisa na tela.
- Atraso típico de 0,2 a 0,5 s na tela. Se travar, escolha qualidade **Leve** no painel.
- O painel só abre no próprio PC. Para liberar para o celular: variável `TVCAST_ALLOW_REMOTE=1`.
- A imagem da tela fica acessível na rede local enquanto você projeta tela/janela. Use em rede de casa.
- Porta diferente: variável `TVCAST_PORT`.

## Pastas

- `server.py`, `web/` — servidor, painel (`panel.html`) e página da TV (`tv.html`)
- `media/` — arquivos de atalho que aparecem no painel
- `android/` — projeto do APK (WebView em tela cheia + tela de IP)

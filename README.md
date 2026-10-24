# TV Cast

O **PC** é o servidor e o controlador. A **TV** (APK) só mostra o que você escolher no PC:
tela inteira, uma janela, um vídeo ou uma imagem.

```
PC  (server.py + painel em http://localhost:8765)  ──Wi-Fi/cabo──▶  TV (APK abre http://IP-do-PC:8765/tv)
```

## 1. No PC

1. Instale o Python 3.9+ (no instalador marque "Add Python to PATH").
2. Dê dois cliques em `run.bat` (Linux: `./run.sh`). Na primeira vez ele instala as dependências.
3. O painel abre sozinho em `http://localhost:8765` e mostra "Na TV digite: 192.168.x.x:8765".
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
4. **Fundo em loop**: no painel, "Escolher vídeo de fundo". Ele roda na TV sem parar. Ao projetar tela, janela, vídeo ou imagem, o fundo pausa; ao parar a projeção (ou a janela fechar) ele volta de onde parou. "Remover fundo" desliga.
5. **Letra / texto**: cole a letra no painel, separe as estrofes com linha em branco e clique (ou use ← →). O texto vai direto para a TV, por cima do fundo, sem atraso. Esc limpa.
6. **Celular como monitor de retorno**: abra `http://IP:8765/tv?lite=1` (sem o vídeo de fundo, bem mais leve).
7. **Página da web (URL)**: cole o endereço (ex.: a página de letras ao vivo) e clique em "Mostrar na TV". Ela fica por cima do fundo e volta ao fundo quando você clicar em "Parar projeção". No app da TV ela abre numa tela própria (sem bloqueio de sites); num navegador comum alguns sites bloqueiam. "Recarregar página" atualiza.
8. **CSS personalizado**: o campo do painel vale para a página da web (no app da TV) e para a letra (`#lyr` e `#lyrIn`). Fica salvo em `settings.json`.
9. Controle remoto da TV: **VOLTAR** na projeção volta para a tela de IP.

## App da TV (versão 1.3)

- **Acha o PC sozinho**: ao abrir, o app procura o PC na rede (UDP, porta 8766) e conecta sem digitar IP. Se o IP do PC mudar, ele reencontra. Se o Firewall perguntar, permita em redes privadas.
- **Marca d'água (logo)** no canto inferior direito: no painel, "Até Conectar" (some quando conecta), "Sempre" ou "Nunca" (importante em apresentação ao vivo). A tela de espera mostra a logo grande e o status ("Procurando o PC..." / "Conectado ao PC").
- **Novo visual** (Figma): tema escuro com variáveis de cor em `:root`, fonte Inter (licença OFL, em `web/assets/fonts`), logo em `web/assets`.
- Se a TV ficar sem memória e o navegador interno cair, o app se reabre sozinho. A tela de páginas da web só usa memória enquanto uma página está projetada.

## Se o vídeo ou a tela engasgar

- Tela/janela usam imagens JPEG: sempre têm algum atraso (0,3 a 1 s) e pesam em aparelho fraco. Para letras, use o card **Letra / texto**.
- Use a qualidade **Mínima** no painel em aparelhos fracos.
- Converta vídeos pesados para 720p: `ffmpeg -i entrada.mkv -vf scale=-2:720 -c:v libx264 -preset fast -crf 23 -c:a aac saida.mp4`

## Captura de janela (estilo OBS)

A janela é capturada diretamente (Windows Graphics Capture): pode ficar atrás de outras janelas, do Alt+Tab ou do menu Iniciar que a TV continua vendo só ela. **Não pode minimizar** (aí a TV fica no último quadro). Se aparecer o aviso "Captura direta indisponível", o `windows-capture` não instalou: rode o `run.bat` de novo com internet. Sem ele, o app usa a cópia da região da tela, que precisa ficar visível.

## Limitações

- **Tela e janela vão sem áudio** (é um fluxo de imagens). Vídeo de arquivo toca com áudio.
- **Vídeo**: a TV toca o que o WebView dela suporta (MP4 H.264, WebM). MKV/AVI podem falhar; a TV avisa na tela.
- Atraso típico de 0,2 a 0,5 s na tela. Se travar, escolha qualidade **Leve** no painel.
- O painel só abre no próprio PC. Para liberar para o celular: variável `TVCAST_ALLOW_REMOTE=1`.
- A imagem da tela fica acessível na rede local enquanto você projeta tela/janela. Use em rede de casa.
- Porta diferente: variável `TVCAST_PORT`.

## Pastas

- `server.py`, `web/` — servidor, painel (`panel.html`) e página da TV (`tv.html`)
- `media/` — arquivos de atalho que aparecem no painel
- `android/` — projeto do APK (WebView em tela cheia + tela de IP)

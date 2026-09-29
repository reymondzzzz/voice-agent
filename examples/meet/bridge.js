(() => {
  const SAMPLE_RATE = 48000;
  const FRAME_SAMPLES = 960;
  const PROCESSOR_SAMPLES = 1024;
  const SPEAKER_POLL_MS = 250;

  let audio = null;

  function toBase64(samples) {
    const bytes = new Uint8Array(samples.buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function fromBase64(encoded) {
    const binary = atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
    return samples;
  }

  function buildAudio() {
    const context = new AudioContext({ sampleRate: SAMPLE_RATE });
    const mixer = context.createGain();

    const capture = context.createScriptProcessor(PROCESSOR_SAMPLES, 1, 1);
    let frame = new Int16Array(FRAME_SAMPLES);
    let filled = 0;
    capture.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      for (let i = 0; i < input.length; i++) {
        frame[filled++] = Math.max(-1, Math.min(1, input[i])) * 32767;
        if (filled === FRAME_SAMPLES) {
          window.vmeetCapture(toBase64(frame));
          frame = new Int16Array(FRAME_SAMPLES);
          filled = 0;
        }
      }
    };
    mixer.connect(capture);
    capture.connect(context.destination);

    const queue = [];
    let head = null;
    let offset = 0;
    const playback = context.createScriptProcessor(PROCESSOR_SAMPLES, 1, 1);
    playback.onaudioprocess = (event) => {
      const output = event.outputBuffer.getChannelData(0);
      for (let i = 0; i < output.length; i++) {
        if (!head || offset === head.length) {
          head = queue.shift() || null;
          offset = 0;
        }
        output[i] = head ? head[offset++] : 0;
      }
    };
    const microphone = context.createMediaStreamDestination();
    playback.connect(microphone);
    // A script processor only runs while the context destination pulls it; the zero gain keeps the agent inaudible locally.
    const pull = context.createGain();
    pull.gain.value = 0;
    playback.connect(pull);
    pull.connect(context.destination);

    return { context, mixer, microphone, queue, holders: [] };
  }

  function ensureAudio() {
    audio = audio || buildAudio();
    if (audio.context.state === "suspended") audio.context.resume();
    return audio;
  }

  function captureRemoteTrack(track) {
    const { context, mixer, holders } = ensureAudio();
    const stream = new MediaStream([track]);
    // Chrome renders a remote WebRTC track into Web Audio as silence unless a media element also consumes it.
    const holder = new Audio();
    holder.muted = true;
    holder.srcObject = stream;
    holders.push(holder);
    context.createMediaStreamSource(stream).connect(mixer);
    track.addEventListener("ended", () => holders.splice(holders.indexOf(holder), 1));
  }

  // With encoded streams available Meet decodes audio in its own worklet and the receiver tracks carry nothing.
  delete RTCRtpReceiver.prototype.createEncodedStreams;

  const NativePeerConnection = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends NativePeerConnection {
    constructor(...args) {
      super(...args);
      this.addEventListener("track", (event) => {
        if (event.track.kind === "audio") captureRemoteTrack(event.track);
      });
    }
  };

  const nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    if (!constraints || !constraints.audio) return nativeGetUserMedia(constraints);
    const stream = new MediaStream(ensureAudio().microphone.stream.getAudioTracks().map((track) => track.clone()));
    if (constraints.video) {
      const camera = await nativeGetUserMedia({ video: constraints.video });
      camera.getVideoTracks().forEach((track) => stream.addTrack(track));
    }
    return stream;
  };

  window.vmeetPlay = (encoded) => {
    ensureAudio().queue.push(fromBase64(encoded));
  };

  // The speaking highlight is a leaf div with a thick border that Meet keeps display:none while the tile is silent.
  function isSpeaking(tile) {
    return [...tile.querySelectorAll("div")].some((div) => {
      if (div.children.length) return false;
      const style = getComputedStyle(div);
      return style.display !== "none" && parseFloat(style.borderTopWidth) > 3;
    });
  }

  function speakingNames() {
    return [...document.querySelectorAll("div[data-participant-id]")]
      .filter(isSpeaking)
      .map((tile) => tile.querySelector("span.notranslate")?.textContent.trim())
      .filter(Boolean);
  }

  let lastSpeakers = "";
  setInterval(() => {
    const names = speakingNames();
    const key = names.join("\n");
    if (key === lastSpeakers) return;
    lastSpeakers = key;
    window.vmeetSpeakers(names);
  }, SPEAKER_POLL_MS);
})();

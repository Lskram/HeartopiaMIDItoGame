# Heartopia MIDI Bin Maker

Small Windows tool for converting one or more MIDI files into one Heartopia record `.bin`.

Confirmed profiles:

- Piano: state `257/1`, f2 `1.0008`
- Cajon: state `259/3`, f2 `1.0773`
- Sax: state `277/21`, f2 `0.4640`

Usage:

1. Run `run_app.cmd`.
2. Add one or more `.mid` files.
3. Pick an instrument for each sequence.
4. Optionally set start offset and transpose.
5. Set the song title and output folder.
6. Press `Build .bin`.

The app auto-detects the output folder from:

```text
%USERPROFILE%\AppData\LocalLow\xd\Heartopia\record\*
```

It chooses the profile folder with the most/recent `.bin` files. Use `Auto detect` if you switch accounts or the folder id changes.

The preview timeline is visual only. It does not play audio.

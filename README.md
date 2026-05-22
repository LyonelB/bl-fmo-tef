# BL-FMO-TEF

FM broadcast monitoring with TEF6686 hardware tuner + HifiBerry DAC+ADC

## Hardware
- TEF6686 hardware FM tuner (USB)
- Raspberry Pi 4
- HifiBerry DAC+ADC (MPX capture 192kHz)

## Stack
- Python 3 / Flask / Gunicorn
- arecord + ffmpeg + Icecast2
- TEF6686 USB serial (RDS, signal level)

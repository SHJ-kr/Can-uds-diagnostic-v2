// [테스트 전용 fake] GPIO 핀 레지스터 접근 흉내 (MODULE_P20.OUT.B.P6 = 0; 패턴 지원)
#ifndef IFXPORT_H
#define IFXPORT_H

typedef enum { IfxPort_OutputMode_pushPull } IfxPort_OutputMode;
typedef enum { IfxPort_InputMode_pullUp } IfxPort_InputMode;
typedef enum { IfxPort_OutputIdx_general } IfxPort_OutputIdx;
typedef enum { IfxPort_PadDriver_cmosAutomotiveSpeed1 } IfxPort_PadDriver;

typedef struct {
    struct {
        struct { int P6; } B;
    } OUT;
} IfxPort_Type;

static IfxPort_Type MODULE_P20;

static inline void IfxPort_setPinModeOutput(IfxPort_Type *port, int pin, IfxPort_OutputMode mode, IfxPort_OutputIdx idx) {
    (void)port; (void)pin; (void)mode; (void)idx;
}

#endif

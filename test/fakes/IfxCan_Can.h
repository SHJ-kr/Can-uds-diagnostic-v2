// [테스트 전용 fake] 실제 IfxCan.h / IfxCan_Can.h 를 대체.
// can.c가 실제로 사용하는 심볼만 최소한으로 재현 (호스트 컴파일용, 실제 하드웨어 동작 없음)
#ifndef IFXCAN_CAN_H
#define IFXCAN_CAN_H

#include "Ifx_Types.h"
#include "IfxPort.h"

// ---- enum류 (값 자체는 의미 없음, 컴파일만 되면 됨) ----
typedef enum { IfxCan_Interrupt_transmissionCompleted, IfxCan_Interrupt_rxFifo0NewMessage } IfxCan_Interrupt;
typedef enum { IfxCan_Status_notSentBusy, IfxCan_Status_sentBusy } IfxCan_Status;
typedef enum { IfxCan_FilterType_range, IfxCan_FilterType_classic } IfxCan_FilterType;
typedef enum { IfxCan_FilterElementConfiguration_storeInRxFifo0 } IfxCan_FilterElementConfiguration;
typedef enum { IfxCan_MessageIdLength_standard } IfxCan_MessageIdLength;
typedef enum { IfxCan_NonMatchingFrame_reject } IfxCan_NonMatchingFrame;
typedef enum { IfxCan_FrameType_transmitAndReceive } IfxCan_FrameType;
typedef enum { IfxCan_RxMode_sharedFifo0 } IfxCan_RxMode;
typedef enum { IfxCan_DataFieldSize_8 } IfxCan_DataFieldSize;
typedef enum { IfxCan_NodeId_0, IfxCan_NodeId_2 } IfxCan_NodeId;
typedef enum { IfxCan_InterruptLine_0, IfxCan_InterruptLine_1 } IfxCan_InterruptLine;
typedef enum { IfxSrc_Tos_cpu0 } IfxSrc_Tos;

// ---- 핀 관련(초기화용 더미 구조체) ----
typedef struct {
    int *txPin;
    IfxPort_OutputMode txMode;
    int *rxPin;
    IfxPort_InputMode rxMode;
    IfxPort_PadDriver padDriver;
} IfxCan_Can_Pins;
static int IfxCan_TXD00_P20_8_OUT;
static int IfxCan_RXD00B_P20_7_IN;
static int IfxCan_TXD02_P15_0_OUT;
static int IfxCan_RXD02A_P15_1_IN;

// ---- 메시지/필터/노드 관련 구조체 (실사용 필드만) ----
typedef struct {
    uint32 messageId;
    int dataLengthCode;
    int readFromRxFifo0;
    int readFromRxFifo1;
} IfxCan_Message;

typedef struct {
    int type;
    uint32 id1;
    uint32 id2;
    int number;
    IfxCan_FilterElementConfiguration elementConfiguration;
} IfxCan_Filter;

typedef struct { int dummy; } IfxCan_Can_Config;
typedef struct { int dummy; } IfxCan_Can;

typedef struct {
    IfxCan_NodeId nodeId;
    const IfxCan_Can_Pins *pins;
    struct { IfxCan_FrameType type; } frame;
    struct { IfxCan_RxMode rxMode; IfxCan_DataFieldSize rxBufferDataFieldSize; IfxCan_DataFieldSize rxFifo0DataFieldSize; int rxFifo0Size; } rxConfig;
    struct { IfxCan_MessageIdLength messageIdLength; int standardListSize; IfxCan_NonMatchingFrame standardFilterForNonMatchingFrames; int rejectRemoteFramesWithStandardId; } filterConfig;
    struct {
        int transmissionCompletedEnabled;
        int rxFifo0NewMessageEnabled;
        struct { IfxCan_InterruptLine interruptLine; IfxSrc_Tos typeOfService; int priority; } traco;
        struct { IfxCan_InterruptLine interruptLine; IfxSrc_Tos typeOfService; int priority; } rxf0n;
    } interruptConfig;
    int busLoopbackEnabled;
    struct { uint32 baudrate; } baudRate;
} IfxCan_Can_NodeConfig;

typedef struct { void *node; } IfxCan_Can_Node;

// ---- can.c가 실제로 호출하는 함수들 (전부 no-op stub) ----
static inline void IfxCan_Can_initModuleConfig(IfxCan_Can_Config *cfg, void *module) { (void)cfg; (void)module; }
static inline void IfxCan_Can_initModule(IfxCan_Can *mod, IfxCan_Can_Config *cfg) { (void)mod; (void)cfg; }
static inline void IfxCan_Can_initNodeConfig(IfxCan_Can_NodeConfig *cfg, IfxCan_Can *mod) { (void)cfg; (void)mod; }
static inline void IfxCan_Can_initNode(IfxCan_Can_Node *node, IfxCan_Can_NodeConfig *cfg) { (void)node; (void)cfg; }
static inline void IfxCan_Can_setStandardFilter(IfxCan_Can_Node *node, IfxCan_Filter *filter) { (void)node; (void)filter; }
static inline void IfxCan_Can_initMessage(IfxCan_Message *msg) { msg->messageId = 0; msg->dataLengthCode = 0; }
static inline void IfxCan_Node_clearInterruptFlag(void *node, IfxCan_Interrupt interrupt) { (void)node; (void)interrupt; }

// ---- [테스트 관측용] 실제로 "전송"된 프레임들을 순서대로 기록 ----
// Can_SendMsg가 호출될 때마다 IfxCan_Can_sendMessage가 불리므로, 여기서 가로채서 기록한다.
#define FAKE_CAN_CAPTURE_MAX 64
typedef struct {
    uint32 id;
    unsigned char data[8];
} FakeCanFrame;

static FakeCanFrame g_fakeCanSentFrames[FAKE_CAN_CAPTURE_MAX];
static int g_fakeCanSentCount = 0;

static inline void FakeCan_ResetCapture(void) { g_fakeCanSentCount = 0; }

// Can_SendMsg가 실제로 CAN 버스에 쓴 "것처럼" 하려면 이 함수가 성공 반환해야 함.
// 테스트에서는 이 함수를 우리가 원하는 대로 만들 수 있도록 전역 카운터만 둠.
// Can_SendMsg의 실제 코드: while (IfxCan_Status_notSentBusy == IfxCan_Can_sendMessage(...)) {}
// 즉 notSentBusy가 반환되는 동안 계속 재시도(busy-wait)하다가, 그 값이 아니면(=전송 성공) 탈출한다.
// 그래서 테스트에서는 매번 "성공"을 의미하는 다른 값을 반환해야 무한루프에 안 빠진다.
static inline IfxCan_Status IfxCan_Can_sendMessage(IfxCan_Can_Node *node, IfxCan_Message *msg, uint32 *data) {
    (void)node;
    if (g_fakeCanSentCount < FAKE_CAN_CAPTURE_MAX) {
        FakeCanFrame *f = &g_fakeCanSentFrames[g_fakeCanSentCount++];
        f->id = msg->messageId;
        const unsigned char *bytes = (const unsigned char *)data;
        for (int i = 0; i < 8; i++) f->data[i] = bytes[i];
    }
    return IfxCan_Status_sentBusy; // notSentBusy가 "아닌" 값 -> 루프 즉시 탈출 (= 전송 성공 취급)
}
static inline void IfxCan_Can_readMessage(IfxCan_Can_Node *node, IfxCan_Message *msg, uint32 *data) {
    (void)node; (void)msg; (void)data;
}

#endif

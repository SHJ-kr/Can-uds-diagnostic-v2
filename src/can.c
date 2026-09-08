#include "can.h"
#include "isr_priority.h"
#include "my_stdio.h"
#include "GPIO.h"
#include "etc.h"
#include "syscfg.h"
#include "ToF.h"
#include "Ultrasonic.h"

#define MAX_DTC_COUNT 16
#define CAN_RECV_CHAR_SIZE 8

static DtcEntry_t g_dtcList[MAX_DTC_COUNT]; // DTC 코드 저장하는 것
static unsigned int g_dtcCount = 0;

static unsigned char tpBuf[256]; // tp 수신 버퍼
static int tpLen = 0;
static int tpExpected = 0;
static int tpActive = 0;
static unsigned char tpNextSn = 1;
static unsigned char result_char = 'F';

// 2. 전역 변수로 ECU 정보 인스턴스 생성 및 초기화
info g_ecuInfo = {
    "MY_TC375_VIN_001",  // VIN (17자)
    "TC375_HW_V1.0.0",   // H/W
    "MY_APP_SW_V1.2.3",  // S/W
    "SN_ECU_1234567890", // Serial
    "MyProjectSupplier"  // Supplier
};

// [신규] 전역 변수 초기화 (기존 C 코드의 하드코딩된 값)
SensorThresholds_t g_sensorThresholds = {
    0,   // ultra_min_mm
    400, // ultra_max_mm
    0,   // tof_min_mm
    5000 // tof_max_mm
};

McmcanType g_mcmcan;

static void UDS_Handle_14(const unsigned char *uds, int len)
{
    // 실제로 DTC 스토리지 초기화하는 코드가 있으면 여기에 추가
    memset(g_dtcList, 0, sizeof(g_dtcList));
    g_dtcCount = 0;
    // ex) memset(dtc_list, 0, sizeof(dtc_list));

    unsigned char resp[8] = {0x02, 0x54, 0xFF, 0, 0, 0, 0, 0};
    Can_SendMsg(0x7E8, (const char *)resp, 8);
}

static void UDS_Handle_19(const unsigned char *uds, int len)
{
    unsigned char txBuf[128] = {0};
    int txLen = 0;

    if (g_dtcCount == 0)
    {
        // ⚙️ DTC가 없을 때: 최소한의 응답 프레임 전송
        txBuf[0] = 0x59; // Positive response SID
        txBuf[1] = 0x02; // Sub-function echo
        txBuf[2] = 0x00; // Status mask (no DTC)
        txLen = 3;

        Can_SendMsg(0x7E8, (const char *)txBuf, 8); // 8바이트 맞춰 전송
        my_printf("[UDS] No DTCs stored (sending empty DTC response)\n");
        return;
    }

    // ⚙️ DTC가 있는 경우 — 기존 로직 유지
    txBuf[0] = 0x59;
    txBuf[1] = 0x02;
    txBuf[2] = 0xFF; // ← Availability Mask 포함 (OK)
    txLen = 3;

    for (int i = 0; i < g_dtcCount; i++)
    {
        unsigned int code = g_dtcList[i].dtcCode;
        txBuf[txLen++] = (code >> 16) & 0xFF;
        txBuf[txLen++] = (code >> 8) & 0xFF;
        txBuf[txLen++] = code & 0xFF;
        txBuf[txLen++] = g_dtcList[i].status; // ← ★ 누락된 Status 추가
    }

    if (txLen > 8)
        Can_TpSend(0x7E8, txBuf, txLen);
    else
        Can_SendMsg(0x7E8, (const char *)txBuf, 8);
}

/* FlowControl(0x30) 전송 (표준 준수/안정성용) */
static inline void Tp_SendFlowControl(void)
{
    unsigned char fc[8] = {0x30, 0x00, 0x00, 0, 0, 0, 0, 0};
    Can_SendMsg(0x7e8, (const char *)fc, 8);
}

/* 0x2E DID=0005 처리: g_ecuInfo 갱신 + Positive Response */
static inline void UDS_Handle_2E_0005(const unsigned char *uds, int len)
{
    // uds[0]=0x2E, uds[1]=0x00, uds[2]=0x05, uds[3..]=payload
    if (len < 3 + (int)sizeof(info))
        return; // 길이 방어
    memcpy(&g_ecuInfo, &uds[3], sizeof(info));

    // 필요시 비휘발성 저장
    // SaveToFlash(&g_ecuInfo);

    unsigned char pos[8] = {0x03, 0x6E, 0x00, 0x05, 0, 0, 0, 0};
    Can_SendMsg(0x7e8, (const char *)pos, 8);
}

// [신규] DID 0x0006 쓰기 요청을 처리하는 함수
static inline void UDS_Handle_2E_0006(const unsigned char *uds, int len)
{
    // 데이터 길이 확인 (SID, DID H/L + 8바이트 데이터)
    if (len < 3 + (int)sizeof(SensorThresholds_t))
    {
        // 길이가 짧으면 Negative Response
        unsigned char neg[8] = {0x03, 0x7F, 0x2E, 0x13, 0, 0, 0, 0}; // Incorrect Message Length
        Can_SendMsg(0x7e8, (const char *)neg, 8);
        return;
    }

    // 전역 변수 g_sensorThresholds를 새 값으로 덮어쓰기
    memcpy(&g_sensorThresholds, &uds[3], sizeof(SensorThresholds_t));

    // Positive Response 전송
    unsigned char pos[8] = {0x03, 0x6E, 0x00, 0x06, 0, 0, 0, 0};
    Can_SendMsg(0x7e8, (const char *)pos, 8);
}

/* ISO-TP로 완성된 UDS 요청 한 건을 처리 (현재는 0x2E 0005만 사용) */
static inline void UDS_Dispatch_CompletedPdu(const unsigned char *uds, int len)
{
    if (len < 1)
        return;
    unsigned char sid = uds[0];

    if (sid == 0x14)
    { // ClearDTC
        UDS_Handle_14(uds, len);
        return;
    }

    if (sid == 0x19)
    { // ReadDTC
        UDS_Handle_19(uds, len);
        return;
    }
    if (sid == 0x2E)
    {
        if (len >= 3 && uds[1] == 0x00 && uds[2] == 0x05)
        {
            UDS_Handle_2E_0005(uds, len);
        }
        else if (len >= 3 && uds[1] == 0x00 && uds[2] == 0x06)
        {
            UDS_Handle_2E_0006(uds, len);
        }
        else
        {
            unsigned char neg[8] = {0x03, 0x7F, 0x2E, 0x31, 0, 0, 0, 0}; // R-O-O-R
            Can_SendMsg(0x7e8, (const char *)neg, 8);
        }
    }
    else
    {
        // 이 경로로 들어오는 다른 SID는 현재 없음 (0x22/0x19/0x14는 기존 로직으로 처리)
        unsigned char neg[8] = {0x03, 0x7F, sid, 0x11, 0, 0, 0, 0}; // Service not supported
        Can_SendMsg(0x7e8, (const char *)neg, 8);
    }
}

/*********************************************************************************************************************/
/*-------------------------------------------------Global variables--------------------------------------------------*/
/*********************************************************************************************************************/
dvmcanType g_mcmcan; /* Global MCMCAN configuration and control structure    */

/*********************************************************************************************************************/
/*---------------------------------------------Function Implementations----------------------------------------------*/
/*********************************************************************************************************************/
/* Default CAN Tx Handler */
IFX_INTERRUPT(Can_TxIsrHandler, 0, ISR_PRIORITY_CAN_TX);
void Can_TxIsrHandler(void)
{
    /* Clear the "Transmission Completed" interrupt flag */
    IfxCan_Node_clearInterruptFlag(g_mcmcan.canSrcNode.node, IfxCan_Interrupt_transmissionCompleted);
}
#ifndef CAN_PROJECT
/* Default CAN Rx Handler */
IFX_INTERRUPT(Can_RxIsrHandler, 0, ISR_PRIORITY_CAN_RX);

void Can_TpRx(const unsigned char *data, int len)
{
    unsigned char pci = data[0] >> 4;

    if (pci == 0x1)
    { // First Frame
        tpExpected = ((data[0] & 0x0F) << 8) | data[1];
        tpLen = 0;
        memcpy(tpBuf, &data[2], 6);
        tpLen = 6;
        tpActive = 1;
        tpNextSn = 1;
        Tp_SendFlowControl();
    }
    else if (pci == 0x2 && tpActive)
    { // Consecutive Frame
        unsigned char sn = data[0] & 0x0F;
        if (sn == tpNextSn)
        {
            memcpy(tpBuf + tpLen, &data[1], 7);
            tpLen += 7;
            tpNextSn = (tpNextSn + 1) & 0x0F;
        }
        if (tpLen >= tpExpected)
        {
            tpActive = 0;
            UDS_Dispatch_CompletedPdu(tpBuf, tpExpected);
        }
    }
}

void DTC_Report(unsigned int code)
{
    // 1) 기존 항목 찾기
    for (unsigned int i = 0; i < g_dtcCount; i++)
    {
        if (g_dtcList[i].dtcCode == code)
        {
            if (g_dtcList[i].detectCnt == 0)
            {
                // 첫 감지 → 보류
                g_dtcList[i].detectCnt = 1;
                g_dtcList[i].status = 0x01; // Pending
            }
            else
            {
                // 두 번째 이상 감지 → 확정
                g_dtcList[i].detectCnt++;
                g_dtcList[i].status = 0x40; // Confirmed
            }
            return;
        }
    }

    // 2) 새 항목 추가 (최초 감지=보류)
    if (g_dtcCount < MAX_DTC_COUNT)
    {
        g_dtcList[g_dtcCount].dtcCode = code;
        g_dtcList[g_dtcCount].detectCnt = 1;
        g_dtcList[g_dtcCount].status = 0x01; // Pending
        g_dtcCount++;
    }
}

void DTC_Add(unsigned int code, unsigned char status)
{
    // 내부 표준은 자동 승급 사용
    DTC_Report(code);

    // 필요 시 호출자가 더 높은 단계(예: 0x40)를 강제로 올려 둘 수 있도록 허용
    for (unsigned int i = 0; i < g_dtcCount; i++)
    {
        if (g_dtcList[i].dtcCode == code)
        {
            if (status > g_dtcList[i].status)
            {
                g_dtcList[i].status = status;
                if (status == 0x40 && g_dtcList[i].detectCnt < 2)
                {
                    g_dtcList[i].detectCnt = 2; // 일관성 유지
                }
            }
            return;
        }
    }
}

void DTC_Clear(void)
{
    memset(g_dtcList, 0, sizeof(g_dtcList));
    g_dtcCount = 0;
}
/* -------------------- ISO15765-2 CAN TP 송신 -------------------- */
void Can_TpSend(unsigned int id, unsigned char *data, int len)
{
    if (len <= 7)
    {
        unsigned char sf[8] = {0};
        sf[0] = 0x00 | len; // SF
        for (int i = 0; i < len; i++)
            sf[i + 1] = data[i];
        Can_SendMsg(id, (const char *)sf, 8);
        return;
    }

    /* First Frame */
    unsigned char ff[8] = {0};
    ff[0] = 0x10 | ((len >> 8) & 0x0F);
    ff[1] = len & 0xFF;
    for (int i = 0; i < 6 && i < len; i++)
        ff[i + 2] = data[i];
    Can_SendMsg(id, (const char *)ff, 8);

    int sent = 6;
    unsigned char sn = 1;
    while (sent < len)
    {
        unsigned char cf[8] = {0};
        cf[0] = 0x20 | (sn & 0x0F);
        for (int i = 1; i < 8 && sent < len; i++, sent++)
            cf[i] = data[sent];
        Can_SendMsg(id, (const char *)cf, 8);
        sn++;
        if (sn > 15)
            sn = 0;
        delay_ms(1); // 약간의 지연 (예: 1ms)
    }
}

/* -------------------- CAN 수신 ISR -------------------- */

IFX_INTERRUPT(Can_RxIsrHandler, 0, ISR_PRIORITY_CAN_RX);
void Can_RxIsrHandler(void)
{
    unsigned int rxID;
    unsigned char rxData[8] = {
        0,
    };
    int rxLen;

    Can_RecvMsg(&rxID, (char *)rxData, &rxLen);

    if (rxID < 0x700)
        return;

    if ((rxData[0] >> 4) >= 1 && (rxData[0] >> 4) <= 2)
    {
        Can_TpRx(rxData, rxLen);
        return;
    }
    unsigned char SID = rxData[1];
    unsigned short DID = ((unsigned short)rxData[2] << 8) | rxData[3];
    unsigned char posSid = SID + 0x40;

    unsigned char negCanData[8] = {0x03, 0x7F, SID, 0x11, 0, 0, 0, 0};

    /* -------------------- WriteDataByIdentifier (0x2E) -------------------- */
    if (SID == 0x2E)
    {
        if (DID == 0x0005)
        {
            // 수신 데이터 길이 검증
            if (rxLen < 8)
            {
                // ISO-TP FirstFrame을 기다려야 하므로 수신 버퍼 쪽에서 조합 완료 후 처리 필요
                // 여기서는 단일 프레임 Write만 처리
                negCanData[3] = 0x13; // Incorrect Message Length
                Can_SendMsg(0x7e8, (const char *)negCanData, 8);
                return;
            }

            // ---- 수신 ISO-TP 전체 데이터를 수집 (멀티프레임 포함) ----
            // 아래는 간단히 예시로, 실제 환경에선 별도 TP 수신 버퍼를 완성 후 memcpy
            // 여기서는 단일 프레임 Write (SF) 또는 조립된 프레임이 들어왔다고 가정
            unsigned char *payload = &rxData[4]; // 0x2E, DID_H, DID_L 이후 데이터 시작
            int dataLen = rxLen - 4;
            if (dataLen > sizeof(info))
                dataLen = sizeof(info);

            memcpy(&g_ecuInfo, payload, dataLen);

            // ✅ 필요시 EEPROM / Flash에 저장
            // SaveToFlash(&g_ecuInfo);

            // Positive Response (0x6E)
            unsigned char pos[8] = {0x04, 0x6E, 0x00, 0x05, 0, 0, 0, 0};
            Can_SendMsg(0x7e8, (const char *)pos, 8);
            return;
        }
        else
        {
            negCanData[3] = 0x31; // Request Out Of Range
            Can_SendMsg(0x7e8, (const char *)negCanData, 8);
            return;
        }
    }

    /* -------------------- ReadDataByIdentifier (0x22) -------------------- */
    else if (SID == 0x22)
    {
        unsigned int senVal = 0;
        bool ok = false;
        int side_index = -1;

        switch (DID)
        {
            case 0x0001:
                side_index = 0;
                break; // Left
            case 0x0002:
                side_index = 1;
                break; // Right
            case 0x0003:
                side_index = 2;
                break; // Rear
            case 0x0004:
                side_index = 3;
                break; // ToF
            case 0x0005:
                side_index = 4;
                break; // ECU_INFO
            default:
                Can_SendMsg(0x7e8, (const char *)negCanData, 8);
                return;
        }

        /* ----- 초음파 센서 (0~2) ----- */
        if (side_index <= 2)
        {
            UltrasonicData_t tmp;
            ok = Ultrasonic_GetLatestData((UltrasonicSide)side_index, &tmp);
            if (ok)
                senVal = tmp.dist_raw_mm;
            result_char = 'P';

            if (!ok)
            {
                DTC_Report(0x010100 + (side_index * 0x10) + 0x0);
                Can_SendMsg(0x7e8, (const char *)negCanData, 8);
                result_char = 'F';
                return;
            }

            if (senVal < g_sensorThresholds.ultra_min_mm || senVal > g_sensorThresholds.ultra_max_mm)
            {
                DTC_Report(0x010100 + (side_index * 0x10) + 0x1);
                result_char = 'F';
            }
        }

        /* ----- ToF 센서 (3) ----- */
        else if (side_index == 3)
        {
            ToFData_t tof;
            ok = ToF_GetLatestData(&tof);
            if (ok)
                senVal = (unsigned int)(tof.distance_m * 1000.0f);
            result_char = 'P';

            if (!ok)
            {
                DTC_Report(0x010200);
                Can_SendMsg(0x7e8, (const char *)negCanData, 8);
                result_char = 'F';
                return;
            }

            if (senVal < g_sensorThresholds.tof_min_mm || senVal > g_sensorThresholds.tof_max_mm)
            {
                DTC_Report(0x010201);
                result_char = 'F';
                my_printf(" totmin: %u, tof max : %u", g_sensorThresholds.tof_min_mm, g_sensorThresholds.tof_max_mm);
            }
        }

        /* ----- ECU Info (DID=0x0005) ----- */
        else if (side_index == 4)
        {
            unsigned char txBuf[sizeof(info) + 3];
            int txLen = 0;

            txBuf[txLen++] = posSid;    // 0x62 (0x22 + 0x40)
            txBuf[txLen++] = rxData[2]; // DID_H
            txBuf[txLen++] = rxData[3]; // DID_L
            memcpy(&txBuf[txLen], &g_ecuInfo, sizeof(info));
            txLen += sizeof(info);

            Can_TpSend(0x7e8, txBuf, txLen);
            return;
        }

        /* ----- 센서 응답 (0x0001~0x0004) ----- */
        unsigned char posCanData[8] = {0x06,          posSid,      rxData[2], rxData[3], (senVal >> 8) & 0xFF,
                                       senVal & 0xFF, result_char, 0};
        Can_SendMsg(0x7e8, (const char *)posCanData, 8);
        return;
    }

    /* -------------------- Read DTC Information (0x19) -------------------- */
    else if (SID == 0x19)
    {
        unsigned char subFunc = rxData[2];
        if (subFunc == 0x02)
        {
            unsigned char txBuf[128];
            int txLen = 0;

            txBuf[txLen++] = 0x59;
            txBuf[txLen++] = subFunc;
            txBuf[txLen++] = 0xFF; // ← ★ Availability Mask 추가

            for (unsigned int i = 0; i < g_dtcCount; i++)
            {
                unsigned int code = g_dtcList[i].dtcCode;
                txBuf[txLen++] = (code >> 16) & 0xFF;
                txBuf[txLen++] = (code >> 8) & 0xFF;
                txBuf[txLen++] = code & 0xFF;
                txBuf[txLen++] = g_dtcList[i].status;
            }

            Can_TpSend(0x7e8, txBuf, txLen);
            return;
        }
    }

    /* -------------------- Clear DTC (0x14) -------------------- */
    else if (SID == 0x14)
    {
        DTC_Clear();
        unsigned char pos[8] = {0x02, 0x54, 0xFF, 0, 0, 0, 0, 0};
        Can_SendMsg(0x7e8, (const char *)pos, 8);
        return;
    }

    /* -------------------- Unknown Service -------------------- */
    else
    {
        Can_SendMsg(0x7e8, (const char *)negCanData, 8);
        return;
    }
}
#endif

/* Function to initialize MCMCAN module and nodes related for this application use case */
void Can_Init(CAN_BAUDRATES ls_baudrate, CAN_NODE CAN_Node)
{
    /* wake up transceiver (node 0) */
    IfxPort_setPinModeOutput(&MODULE_P20, 6, IfxPort_OutputMode_pushPull, IfxPort_OutputIdx_general);
    MODULE_P20.OUT.B.P6 = 0;

    IfxCan_Can_initModuleConfig(&g_mcmcan.canConfig, &MODULE_CAN0);
    IfxCan_Can_initModule(&g_mcmcan.canModule, &g_mcmcan.canConfig);
    IfxCan_Can_initNodeConfig(&g_mcmcan.canNodeConfig, &g_mcmcan.canModule);
    switch (ls_baudrate)
    {
        case BD_NOUSE:
            g_mcmcan.canNodeConfig.busLoopbackEnabled = TRUE;
            break;
        case BD_500K:
            g_mcmcan.canNodeConfig.baudRate.baudrate = 500000;
            break;
        case BD_1M:
            g_mcmcan.canNodeConfig.baudRate.baudrate = 1000000;
            break;
    }
    g_mcmcan.canNodeConfig.busLoopbackEnabled = FALSE;

    if (CAN_Node == CAN_NODE0)
    { /* CAN Node 0 for lite kit */
        g_mcmcan.canNodeConfig.nodeId = IfxCan_NodeId_0;
        const IfxCan_Can_Pins pins = {
            &IfxCan_TXD00_P20_8_OUT, IfxPort_OutputMode_pushPull, /* TX Pin for lite kit (can node 0) */
            &IfxCan_RXD00B_P20_7_IN, IfxPort_InputMode_pullUp,    /* RX Pin for lite kit (can node 0) */
            IfxPort_PadDriver_cmosAutomotiveSpeed1};
        g_mcmcan.canNodeConfig.pins = &pins;
    }
    else if (CAN_Node == CAN_NODE2)
    { /* CAN Node 2 for mikrobus */
        g_mcmcan.canNodeConfig.nodeId = IfxCan_NodeId_2;
        const IfxCan_Can_Pins pins = {
            &IfxCan_TXD02_P15_0_OUT, IfxPort_OutputMode_pushPull, /* TX Pin for mikrobus (can node 2) */
            &IfxCan_RXD02A_P15_1_IN, IfxPort_InputMode_pullUp,    /* RX Pin for mikrobus (can node 2) */
            IfxPort_PadDriver_cmosAutomotiveSpeed1};
        g_mcmcan.canNodeConfig.pins = &pins;
    }

    g_mcmcan.canNodeConfig.frame.type = IfxCan_FrameType_transmitAndReceive;
    g_mcmcan.canNodeConfig.interruptConfig.transmissionCompletedEnabled = TRUE;
    g_mcmcan.canNodeConfig.interruptConfig.traco.priority = ISR_PRIORITY_CAN_TX;
    g_mcmcan.canNodeConfig.interruptConfig.traco.interruptLine = IfxCan_InterruptLine_0;
    g_mcmcan.canNodeConfig.interruptConfig.traco.typeOfService = IfxSrc_Tos_cpu0;
    IfxCan_Can_initNode(&g_mcmcan.canSrcNode, &g_mcmcan.canNodeConfig);

    /* Reception handling configuration */
    g_mcmcan.canNodeConfig.rxConfig.rxMode = IfxCan_RxMode_sharedFifo0;
    g_mcmcan.canNodeConfig.rxConfig.rxBufferDataFieldSize = IfxCan_DataFieldSize_8;
    g_mcmcan.canNodeConfig.rxConfig.rxFifo0DataFieldSize = IfxCan_DataFieldSize_8;
    g_mcmcan.canNodeConfig.rxConfig.rxFifo0Size = 15;
    /* General filter configuration */
    g_mcmcan.canNodeConfig.filterConfig.messageIdLength = IfxCan_MessageIdLength_standard;
    g_mcmcan.canNodeConfig.filterConfig.standardListSize = 8;
    g_mcmcan.canNodeConfig.filterConfig.standardFilterForNonMatchingFrames = IfxCan_NonMatchingFrame_reject;
    g_mcmcan.canNodeConfig.filterConfig.rejectRemoteFramesWithStandardId = TRUE;
    /* Interrupt configuration */
    g_mcmcan.canNodeConfig.interruptConfig.rxFifo0NewMessageEnabled = TRUE;
    g_mcmcan.canNodeConfig.interruptConfig.rxf0n.priority = ISR_PRIORITY_CAN_RX;
    g_mcmcan.canNodeConfig.interruptConfig.rxf0n.interruptLine = IfxCan_InterruptLine_1;
    g_mcmcan.canNodeConfig.interruptConfig.rxf0n.typeOfService = IfxSrc_Tos_cpu0;
    IfxCan_Can_initNode(&g_mcmcan.canDstNode, &g_mcmcan.canNodeConfig);

    /* Rx filter configuration (default: all messages accepted) */
    Can_SetFilterRange(0x0, 0x7FF);
}

void Can_SetFilterRange(uint32 start, uint32 end)
{
    g_mcmcan.canFilter.number = 0;
    g_mcmcan.canFilter.type = IfxCan_FilterType_range;
    g_mcmcan.canFilter.elementConfiguration = IfxCan_FilterElementConfiguration_storeInRxFifo0;
    g_mcmcan.canFilter.id1 = start;
    g_mcmcan.canFilter.id2 = end;
    IfxCan_Can_setStandardFilter(&g_mcmcan.canDstNode, &g_mcmcan.canFilter);
}

void Can_SetFilterMask(uint32 id, uint32 mask)
{
    g_mcmcan.canFilter.number = 0;
    g_mcmcan.canFilter.type = IfxCan_FilterType_classic;
    g_mcmcan.canFilter.elementConfiguration = IfxCan_FilterElementConfiguration_storeInRxFifo0;
    g_mcmcan.canFilter.id1 = id;
    g_mcmcan.canFilter.id2 = mask;
    IfxCan_Can_setStandardFilter(&g_mcmcan.canDstNode, &g_mcmcan.canFilter);
}

void Can_SendMsg(unsigned int id, const char *txData, int len)
{
    /* Initialization of the TX message with the default configuration */
    IfxCan_Can_initMessage(&g_mcmcan.txMsg);

    g_mcmcan.txMsg.messageId = id;
    g_mcmcan.txMsg.dataLengthCode = len;

    /* Define the content of the data to be transmitted */
    for (int i = 0; i < 8; i++)
    {
        g_mcmcan.txData[i] = txData[i];
    }

    /* Send the CAN message with the previously defined TX message content */
    while (IfxCan_Status_notSentBusy ==
           IfxCan_Can_sendMessage(&g_mcmcan.canSrcNode, &g_mcmcan.txMsg, (uint32 *)&g_mcmcan.txData[0]))
    {
    }
}

int Can_RecvMsg(unsigned int *id, char *rxData, int *len)
{
    int err = 0;
    /* Clear the "RX FIFO 0 new message" interrupt flag */
    IfxCan_Node_clearInterruptFlag(g_mcmcan.canDstNode.node, IfxCan_Interrupt_rxFifo0NewMessage);

    /* Received message content should be updated with the data stored in the RX FIFO 0 */
    g_mcmcan.rxMsg.readFromRxFifo0 = TRUE;
    g_mcmcan.rxMsg.readFromRxFifo1 = FALSE;

    /* Read the received CAN message */
    IfxCan_Can_readMessage(&g_mcmcan.canDstNode, &g_mcmcan.rxMsg, (uint32 *)&g_mcmcan.rxData);

    *id = g_mcmcan.rxMsg.messageId;
    for (int i = 0; i < 8; i++)
    {
        rxData[i] = g_mcmcan.rxData[i];
    }
    *len = g_mcmcan.rxMsg.dataLengthCode;

    return err;
}

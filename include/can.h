#ifndef CAN_H_
#define CAN_H_ 1

/*********************************************************************************************************************/
/*-----------------------------------------------------Includes------------------------------------------------------*/
/*********************************************************************************************************************/
#include <stdio.h>
#include <string.h>
#include "Ifx_Types.h"
#include "IfxCan_Can.h"
#include "IfxCan.h"
#include "IfxCpu_Irq.h"
#include "IfxPort.h"                                        /* For GPIO Port Pin Control                            */
#include "Ultrasonic.h"
/*********************************************************************************************************************/
/*------------------------------------------------------Macros-------------------------------------------------------*/
/*********************************************************************************************************************/
#define CAN_MESSAGE_ID              (uint32)0x777           /* Message ID that will be used in arbitration phase    */
#define MAXIMUM_CAN_DATA_PAYLOAD    2                       /* Define maximum classical CAN payload in 4-byte words */

/*********************************************************************************************************************/
/*--------------------------------------------------Data Structures--------------------------------------------------*/
/*********************************************************************************************************************/
typedef struct
{
    IfxCan_Can_Config canConfig;                            /* CAN module configuration structure                   */
    IfxCan_Can canModule;                                   /* CAN module handle                                    */
    IfxCan_Can_Node canSrcNode;                             /* CAN source node handle data structure                */
    IfxCan_Can_Node canDstNode;                             /* CAN destination node handle data structure           */
    IfxCan_Can_NodeConfig canNodeConfig;                    /* CAN node configuration structure                     */
    IfxCan_Filter canFilter;                                /* CAN filter configuration structure                   */
    IfxCan_Message txMsg;                                   /* Transmitted CAN message structure                    */
    IfxCan_Message rxMsg;                                   /* Received CAN message structure                       */
    uint8 txData[8];                                        /* Transmitted CAN data array                           */
    uint8 rxData[8];                                        /* Received CAN data array                              */
} McmcanType;

/* [추가] ECU 상세 정보 구조체 */
typedef struct EcuDetails {
    char vin[18];                  // VIN (17바이트 + NULL)
    char hardwarePartNumber[20];   // H/W 부품 번호 (19바이트 + NULL)
    char softwarePartNumber[20];   // S/W 부품 번호 (19바이트 + NULL)
    char serialNumber[20];         // ECU 시리얼 번호 (19바이트 + NULL)
    char supplier[20];             // 공급사 (19바이트 + NULL)
} info;


typedef enum {
    BD_NOUSE = 0,
    BD_500K = 1,
    BD_1M = 2
} CAN_BAUDRATES;

typedef enum {
    CAN_NODE0 = 0, /* CAN Node 0 for lite kit */
    CAN_NODE2 = 2  /* CAN Node 2 for mikrobus */
} CAN_NODE;

typedef struct {
    unsigned int dtcCode;    // 예: 0x010118 (P0118)
    unsigned char status;
    unsigned char  detectCnt;// 예: 0x40 = Confirmed DTC
} DtcEntry_t;

typedef struct {
    unsigned short ultra_min_mm;
    unsigned short ultra_max_mm;
    unsigned short tof_min_mm;
    unsigned short tof_max_mm;
} SensorThresholds_t;

/*********************************************************************************************************************/
/*--------------------------------------------------Global variables-------------------------------------------------*/
/*********************************************************************************************************************/
/* [추가] can.c에 정의된 전역 변수 선언 */
extern McmcanType g_mcmcan;
extern info g_ecuInfo;

/*********************************************************************************************************************/
/*-----------------------------------------------Function Prototypes-------------------------------------------------*/
/*********************************************************************************************************************/
void Can_Init(CAN_BAUDRATES ls_baudrate, CAN_NODE CAN_Node);
void Can_SetFilterRange(uint32 start, uint32 end);
void Can_SetFilterMask(uint32 id, uint32 mask);

void Can_SendMsg(unsigned int id, const char *txData, int len);
int Can_RecvMsg(unsigned int *id, char *rxData, int *len);


void DTC_Report(unsigned int code);
void DTC_Clear(void);

/* [추가] CAN TP 송신 함수 프로토타입 */
void Can_TpSend(unsigned int id, unsigned char *data, int len);

/* [삭제] Can_sendUDS(unsigned char* chArr); // .c 파일에 구현이 없음 */


#endif /* CAN_H_ */

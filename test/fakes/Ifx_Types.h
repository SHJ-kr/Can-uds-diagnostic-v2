// [테스트 전용 fake] 실제 Infineon SDK의 Ifx_Types.h를 대체.
// 리눅스 호스트에서 can.c를 컴파일하기 위한 최소한의 타입만 정의.
#ifndef IFX_TYPES_H
#define IFX_TYPES_H

#include <stdint.h>

typedef uint8_t uint8;
typedef uint16_t uint16;
typedef uint32_t uint32;
typedef int32_t sint32;

#ifndef TRUE
#define TRUE 1
#endif
#ifndef FALSE
#define FALSE 0
#endif

typedef int boolean;

#endif

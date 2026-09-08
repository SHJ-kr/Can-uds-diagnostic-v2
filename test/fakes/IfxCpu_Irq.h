// [테스트 전용 fake] 인터럽트 벡터 등록 매크로 - 호스트에서는 그냥 전방선언으로 대체
#ifndef IFXCPU_IRQ_H
#define IFXCPU_IRQ_H
#define IFX_INTERRUPT(func, vector, prio) void func(void)
#endif

// can.c 실제 검증 코드
// -----------------------------------------------------------------------
// can.c를 직접 #include해서, static 함수(UDS_Handle_14 등)까지 포함한
// 실제 로직 전부를 테스트합니다. (정적 함수를 테스트할 때 흔히 쓰는 방법)
//
// 하드웨어(IfxCan_Can_* 등)는 test/fakes/ 안의 가짜 SDK로 대체되어 있어서
// 실제 CAN 버스나 TC375 보드 없이 로직만 검증합니다.
#include <gtest/gtest.h>
#include "../src/can.c"

// ============================================================
// 공통 유틸: 매 테스트마다 상태 초기화
// ============================================================
class CanTest : public ::testing::Test {
protected:
    void SetUp() override {
        DTC_Clear();
        FakeCan_ResetCapture();
    }
};

// ============================================================
// 1) DTC_Report / DTC_Add / DTC_Clear 로직 검증
// ============================================================

TEST_F(CanTest, DTC_Report_FirstDetection_IsPending) {
    DTC_Report(0x010101);
    ASSERT_EQ(g_dtcCount, 1u);
    EXPECT_EQ(g_dtcList[0].dtcCode, 0x010101u);
    EXPECT_EQ(g_dtcList[0].status, 0x01);   // 최초 감지 = Pending
    EXPECT_EQ(g_dtcList[0].detectCnt, 1);
}

TEST_F(CanTest, DTC_Report_SecondDetection_BecomesConfirmed) {
    DTC_Report(0x010101);
    DTC_Report(0x010101);  // 같은 코드 두 번째 감지
    ASSERT_EQ(g_dtcCount, 1u);   // 새 항목이 아니라 기존 항목이 갱신되어야 함
    EXPECT_EQ(g_dtcList[0].status, 0x40);   // Confirmed
    EXPECT_EQ(g_dtcList[0].detectCnt, 2);
}

TEST_F(CanTest, DTC_Report_DifferentCodes_CreateSeparateEntries) {
    DTC_Report(0x010101);
    DTC_Report(0x010111);
    ASSERT_EQ(g_dtcCount, 2u);
    EXPECT_EQ(g_dtcList[0].dtcCode, 0x010101u);
    EXPECT_EQ(g_dtcList[1].dtcCode, 0x010111u);
}

TEST_F(CanTest, DTC_Report_ExceedsMaxCount_IsIgnored) {
    for (int i = 0; i < MAX_DTC_COUNT + 5; i++) {
        DTC_Report(0x010100 + i);  // 서로 다른 코드로 최대치 초과 시도
    }
    EXPECT_EQ(g_dtcCount, static_cast<unsigned int>(MAX_DTC_COUNT));  // 더 안 늘어나야 함
}

TEST_F(CanTest, DTC_Add_DoesNotDowngradeStatus) {
    DTC_Report(0x010101);
    DTC_Report(0x010101);  // Confirmed(0x40) 상태로 만들어둠
    ASSERT_EQ(g_dtcList[0].status, 0x40);

    DTC_Add(0x010101, 0x01);  // 더 낮은 상태(Pending)로 강제하려는 시도
    EXPECT_EQ(g_dtcList[0].status, 0x40) << "이미 Confirmed인 DTC가 Pending으로 강등되면 안 됨";
}

TEST_F(CanTest, DTC_Add_ForcesHigherStatus_AndFixesDetectCnt) {
    DTC_Report(0x010101);  // detectCnt=1, status=Pending(0x01)
    DTC_Add(0x010101, 0x40);  // 강제로 Confirmed 승격
    EXPECT_EQ(g_dtcList[0].status, 0x40);
    EXPECT_GE(g_dtcList[0].detectCnt, 2) << "Confirmed면 detectCnt도 2 이상으로 일관성 있어야 함";
}

TEST_F(CanTest, DTC_Clear_ResetsEverything) {
    DTC_Report(0x010101);
    DTC_Report(0x010111);
    DTC_Clear();
    EXPECT_EQ(g_dtcCount, 0u);
}

// ============================================================
// 2) Can_TpSend (ISO-TP 송신 프레이밍) 검증
// ============================================================

TEST_F(CanTest, CanTpSend_ShortData_SendsSingleFrame) {
    unsigned char data[3] = {0xAA, 0xBB, 0xCC};
    Can_TpSend(0x7E8, data, 3);

    ASSERT_EQ(g_fakeCanSentCount, 1) << "7바이트 이하는 SF 한 프레임으로 끝나야 함";
    EXPECT_EQ(g_fakeCanSentFrames[0].id, 0x7E8u);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[0], 0x03) << "SF PCI = 데이터 길이(3)";
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0xAA);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[2], 0xBB);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[3], 0xCC);
}

TEST_F(CanTest, CanTpSend_LongData_SplitsIntoFF_And_CF) {
    // 10바이트 -> FF(6바이트) + CF(4바이트), 총 2프레임 기대
    unsigned char data[10];
    for (int i = 0; i < 10; i++) data[i] = static_cast<unsigned char>(i + 1);

    Can_TpSend(0x7E8, data, 10);

    ASSERT_EQ(g_fakeCanSentCount, 2);

    // First Frame 검증: PCI 상위니블=1, 하위+다음바이트=길이(10)
    EXPECT_EQ(g_fakeCanSentFrames[0].data[0] & 0xF0, 0x10);
    EXPECT_EQ(((g_fakeCanSentFrames[0].data[0] & 0x0F) << 8) | g_fakeCanSentFrames[0].data[1], 10);
    for (int i = 0; i < 6; i++) {
        EXPECT_EQ(g_fakeCanSentFrames[0].data[2 + i], data[i]) << "FF의 " << i << "번째 데이터 바이트";
    }

    // Consecutive Frame 검증: PCI 상위니블=2, SN=1, 나머지 4바이트
    EXPECT_EQ(g_fakeCanSentFrames[1].data[0], 0x21) << "CF PCI(0x2) + SN(1)";
    for (int i = 0; i < 4; i++) {
        EXPECT_EQ(g_fakeCanSentFrames[1].data[1 + i], data[6 + i]) << "CF의 " << i << "번째 데이터 바이트";
    }
}

TEST_F(CanTest, CanTpSend_ExactlySevenBytes_StillSingleFrame) {
    unsigned char data[7] = {1, 2, 3, 4, 5, 6, 7};
    Can_TpSend(0x7E8, data, 7);
    ASSERT_EQ(g_fakeCanSentCount, 1) << "정확히 7바이트는 경계값 - SF로 처리되어야 함";
}

// ============================================================
// 3) UDS_Dispatch_CompletedPdu (static 함수) 검증
// ============================================================

TEST_F(CanTest, UdsDispatch_ClearDTC_SendsPositiveResponse) {
    unsigned char req[1] = {0x14};
    UDS_Dispatch_CompletedPdu(req, 1);

    ASSERT_EQ(g_fakeCanSentCount, 1);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0x54) << "0x14 ClearDTC의 Positive Response는 0x54";
}

// [알려진 버그] UDS_Handle_19의 "DTC 없음"/"소수 DTC" 분기는 ISO-TP SF PCI 바이트
// (data[0]에 와야 할 길이 헤더)를 안 붙이고 Can_SendMsg를 직접 호출합니다.
// UDS_Handle_14(ClearDTC)는 {0x02, 0x54, 0xFF, ...}처럼 PCI 바이트(0x02)를 제대로 붙이는데,
// 여긴 txBuf[0]에 UDS SID(0x59)를 바로 써버려서 클라이언트가 정상 파싱을 못 합니다.
// 이 테스트는 "이래야 맞다(0x59 앞에 PCI 바이트 0x03이 와야 한다)"는 정답 기준으로 작성되어 있어서,
// 지금은 실패하는 게 정상입니다 - 버그를 고치면 이 테스트가 통과하게 됩니다.
TEST_F(CanTest, UdsDispatch_ReadDTC_NoDtcs_SendsEmptyResponse) {
    unsigned char req[1] = {0x19};
    UDS_Dispatch_CompletedPdu(req, 1);

    ASSERT_EQ(g_fakeCanSentCount, 1);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[0], 0x03) << "SF PCI 바이트(길이=3)가 먼저 와야 함 - 현재 버그로 실패함";
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0x59);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[2], 0x00) << "DTC 없으면 status mask 0x00";
}

TEST_F(CanTest, UdsDispatch_Write0005_CorrectLength_SendsAck) {
    unsigned char req[3 + sizeof(info)] = {0};
    req[0] = 0x2E; req[1] = 0x00; req[2] = 0x05;
    UDS_Dispatch_CompletedPdu(req, sizeof(req));

    ASSERT_EQ(g_fakeCanSentCount, 1);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0x6E);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[2], 0x00);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[3], 0x05);
}

TEST_F(CanTest, UdsDispatch_Write_UnknownDid_SendsNegative_RequestOutOfRange) {
    unsigned char req[3] = {0x2E, 0x00, 0x99};  // 존재하지 않는 DID
    UDS_Dispatch_CompletedPdu(req, 3);

    ASSERT_EQ(g_fakeCanSentCount, 1);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0x7F);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[3], 0x31) << "requestOutOfRange 기대";
}

TEST_F(CanTest, UdsDispatch_UnknownSid_SendsNegative_ServiceNotSupported) {
    unsigned char req[1] = {0x99};  // 존재하지 않는 서비스
    UDS_Dispatch_CompletedPdu(req, 1);

    ASSERT_EQ(g_fakeCanSentCount, 1);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[1], 0x7F);
    EXPECT_EQ(g_fakeCanSentFrames[0].data[3], 0x11) << "serviceNotSupported 기대";
}

// ============================================================
// 4) Can_TpRx (ISO-TP 수신 재조립) 검증 - FF+CF로 나눠 보낸 걸 잘 합치는지
// ============================================================

TEST_F(CanTest, CanTpRx_ReassemblesMultiFrame_AndDispatches) {
    // 0x2E 0005 write 요청(총 3 + sizeof(info) 바이트)을 FF+CF로 나눠서 주입
    const int totalLen = 3 + static_cast<int>(sizeof(info));
    unsigned char full[3 + sizeof(info)] = {0};
    full[0] = 0x2E; full[1] = 0x00; full[2] = 0x05;
    // VIN 자리에 테스트용 문자열을 넣어 실제로 g_ecuInfo가 갱신되는지 확인
    const char *testVin = "TEST_VIN";
    memcpy(&full[3], testVin, strlen(testVin) + 1);

    // First Frame: PCI(0x10|len_high), len_low, 데이터 6바이트
    unsigned char ff[8];
    ff[0] = 0x10 | ((totalLen >> 8) & 0x0F);
    ff[1] = totalLen & 0xFF;
    memcpy(&ff[2], &full[0], 6);
    Can_TpRx(ff, 8);

    // 나머지를 7바이트씩 Consecutive Frame으로
    int sent = 6;
    unsigned char sn = 1;
    while (sent < totalLen) {
        unsigned char cf[8] = {0};
        cf[0] = static_cast<unsigned char>(0x20 | (sn & 0x0F));
        int chunk = std::min(7, totalLen - sent);
        memcpy(&cf[1], &full[sent], chunk);
        Can_TpRx(cf, 8);
        sent += chunk;
        sn = (sn + 1) % 16;
    }

    // 재조립 후 UDS_Handle_2E_0005가 호출되어 g_ecuInfo가 갱신되고 ACK가 나가야 함.
    // 참고: FF를 받으면 Can_TpRx가 먼저 FlowControl(0x30)을 보내고, 재조립 완료 후 ACK(0x6E)를
    // 또 보내므로 총 2프레임이 나가는 게 정상입니다 (FC + ACK).
    EXPECT_STREQ(g_ecuInfo.vin, testVin);
    ASSERT_EQ(g_fakeCanSentCount, 2) << "FlowControl(1번째) + ACK(2번째), 총 2프레임 기대";
    EXPECT_EQ(g_fakeCanSentFrames[0].data[0] & 0xF0, 0x30) << "1번째는 FlowControl";
    EXPECT_EQ(g_fakeCanSentFrames[1].data[1], 0x6E) << "2번째가 실제 ACK";
}

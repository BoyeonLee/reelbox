"""가져오기 단계: 플랫폼별 내보내기 파일·목록을 items 로 넣는다."""

from pydantic import BaseModel

from reelbox.db import AddOutcome


class ImportReport(BaseModel):
    """가져오기 결과 개수. 끝나면 '새로 추가 N, 중복 M, 인식 실패 K' 로 보여준다."""

    added: int = 0
    duplicate: int = 0
    unrecognized: int = 0
    deleted: int = 0  # 이미 삭제한 영상이라 건너뜀

    def record(self, outcome: AddOutcome) -> None:
        match outcome:
            case AddOutcome.ADDED:
                self.added += 1
            case AddOutcome.DUPLICATE:
                self.duplicate += 1
            case AddOutcome.UNRECOGNIZED:
                self.unrecognized += 1
            case AddOutcome.DELETED:
                self.deleted += 1

    def summary(self) -> str:
        text = f"새로 추가 {self.added}, 중복 {self.duplicate}, 인식 실패 {self.unrecognized}"
        if self.deleted:
            text += f", 이미 삭제한 영상 {self.deleted}"
        return text

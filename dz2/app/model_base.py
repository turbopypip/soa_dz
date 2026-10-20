from decimal import Decimal
from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    # OpenAPI decimal is a JSON number; Pydantic's default would emit a string.
    model_config = ConfigDict(json_encoders={Decimal: float})

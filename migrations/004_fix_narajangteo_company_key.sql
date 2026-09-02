-- 나라장터 전처리 데이터는 한 사업자가 복수 업종·품목을 가질 수 있다.
-- 사업자번호 단독 PK는 정상 레코드 2,525건을 유실하므로 실제 데이터의
-- 고유 단위인 (사업자번호, 대표세부품명번호, 대표업종)으로 변경한다.

DO $$
DECLARE
    current_pk_name TEXT;
    current_pk_columns TEXT[];
BEGIN
    SELECT
        constraint_row.conname,
        array_agg(attribute_row.attname ORDER BY key_column.ordinality)
    INTO current_pk_name, current_pk_columns
    FROM pg_constraint AS constraint_row
    JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
        ON true
    JOIN pg_attribute AS attribute_row
        ON attribute_row.attrelid = constraint_row.conrelid
       AND attribute_row.attnum = key_column.attnum
    WHERE constraint_row.conrelid = 'procurement.narajangteo_company_info'::regclass
      AND constraint_row.contype = 'p'
    GROUP BY constraint_row.conname;

    IF current_pk_columns IS DISTINCT FROM ARRAY['bizno', 'main_item_no', 'main_biz_type'] THEN
        IF EXISTS (
            SELECT 1
            FROM procurement.narajangteo_company_info
            WHERE main_item_no IS NULL OR main_biz_type IS NULL
        ) THEN
            RAISE EXCEPTION
                'Cannot migrate narajangteo key: main_item_no or main_biz_type contains NULL';
        END IF;

        IF current_pk_name IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE procurement.narajangteo_company_info DROP CONSTRAINT %I',
                current_pk_name
            );
        END IF;

        ALTER TABLE procurement.narajangteo_company_info
            ALTER COLUMN main_item_no SET NOT NULL,
            ALTER COLUMN main_biz_type SET NOT NULL;

        ALTER TABLE procurement.narajangteo_company_info
            ADD CONSTRAINT pk_narajangteo_company_info
            PRIMARY KEY (bizno, main_item_no, main_biz_type);
    END IF;
END
$$;

COMMENT ON TABLE procurement.narajangteo_company_info IS
    'Preprocessed Narajangteo company, representative item, and business-type reference rows.';

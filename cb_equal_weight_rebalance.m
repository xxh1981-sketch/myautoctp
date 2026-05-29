% 第1天：对全部 conditionA 等市值买入。
% 第2天起：先卖出不满足 conditionB 的；再对「有持仓(已满足B) ∪ 无持仓且满足A」等市值调仓。
clear;
load dt260515;
x = Premium / 100;
y = x * 100;
Index = y;

[T, N] = size(B);

% 筛选参数（conditionB 各维宽于 conditionA，故 A=>B）
B0 = 160;
B1 = 10000;
B2 = 10000;
His0 = 0;
Dis0 = 50;
Tr0 = 0.1;
Premium0 = 10;
Index0 = 200;

buy_cost = 0.000;
sell_cost = 0.000;
initial_cash = 1000;

dtr = ones(T, 1) * datenum(redemptiondate)' - datenum(date) * ones(1, N);

positions = zeros(T, N);
cash = zeros(T, 1);
total_assets = zeros(T, 1);
cash(1) = initial_cash;
nt = zeros(T, 1);
codeheld = strings(T, 1);
hpmean = zeros(T, 1);
hpmean(1) = 1000;

for t = 1:T
    conditionA = (B(t, :) > B0) & (B(t, :) < B1) & (His(t, :) > His0) & ...
        (Dis(t, :) < Dis0) & (Tr(t, :) > Tr0) & (Premium(t, :) < Premium0) & ...
        (Life(t, :) > 0.5) & (dtr(t, :) > 10 | isnan(dtr(t, :))) & (Index(t, :) < Index0);

    conditionB = (B(t, :) > B0 - 5) & (B(t, :) < B2) & (His(t, :) > His0) & ...
        (Dis(t, :) < Dis0 + 5) & (Tr(t, :) > Tr0) & (Premium(t, :) < Premium0 + 2) & ...
        (dtr(t, :) > 8 | isnan(dtr(t, :))) & (Index(t, :) < Index0 + 3);

    n_trades = 0;

    if t == 1
        current_cash = initial_cash;
        positions(t, :) = 0;
    else
        positions(t, :) = positions(t - 1, :);
        current_cash = cash(t - 1);

        held = find(positions(t, :) > 0);
        for j = 1:length(held)
            idx = held(j);
            if ~conditionB(idx)
                qty = positions(t, idx);
                current_cash = current_cash + qty * B(t, idx) * (1 - sell_cost);
                positions(t, idx) = 0;
                n_trades = n_trades + 1;
            end
        end
    end

    % 调仓池 = 剩余持仓(卖B后均满足B) + 空仓且满足A(新进)
    selected = find(positions(t, :) > 0 | conditionA);
    [positions(t, :), current_cash, n_trades] = rebalance_equal_weight( ...
        t, selected, positions(t, :), current_cash, B, buy_cost, sell_cost, n_trades);

    cash(t) = current_cash;
    total_assets(t) = cash(t) + sum(positions(t, :) .* B(t, :));
    nt(t) = n_trades;

    held_codes = code(positions(t, :) > 0);
    if isempty(held_codes)
        codeheld(t) = string(date(t));
    else
        codeheld(t) = strcat(string(date(t)), ',', strjoin(held_codes));
    end

    if t < T
        xx = find(conditionA);
        if ~isempty(xx)
            hpmean(t + 1) = hpmean(t) * mean(B(t + 1, xx) ./ B(t, xx));
        else
            hpmean(t + 1) = hpmean(t);
        end
    end
end

nt = nt';

daily_holdings = sum(positions > 0, 2);
daily_prices = B;
daily_quantities = positions;
daily_total_assets = total_assets;

asset = total_assets;
for j = 1:length(asset)
    dz(j) = max(asset(1:j));
    xz(j) = min(asset(j:end));
    hc(j) = xz(j) / dz(j) - 1;
end
ndhc = min(asset(241:end) ./ asset(1:end-240) - 1);
% 二者含义不同，不应逐日相等；若曲线重合多半是走势相近或量级接近
fprintf('asset 与 hpmean 最大绝对差： %.4f\n', max(abs(asset(:) - hpmean(:))));
fprintf('asset 与 hpmean 相关系数： %.4f\n', corr(asset(:), hpmean(:)));

figure('Name', '策略净值 vs 高价池（双轴）');
yyaxis left;
plot(date, asset(:), 'LineWidth', 1.2);
ylabel('策略净值 asset');
yyaxis right;
plot(date, hpmean(:), 'LineWidth', 1.2, 'LineStyle', '--', 'Color', [0.85 0.33 0.1]);
ylabel('高价池均值 hpmean');
xlabel('日期');
title('策略净值 vs 高价池被动基准（左=实盘模拟，右=全A等权涨跌链）');
legend('策略净值', '高价池均值', 'Location', 'best');
grid on;

% 归一化到首日=1，便于看走势差异（单轴）
figure('Name', '归一化走势对比');
plot(date, asset(:) / asset(1), 'LineWidth', 1.2, 'DisplayName', '策略净值');
hold on;
plot(date, hpmean(:) / hpmean(1), '--', 'LineWidth', 1.2, 'DisplayName', '高价池均值');
hold off;
ylabel('相对首日');
xlabel('日期');
title('归一化对比（若仍几乎重合说明策略近似跟踪全A等权）');
legend('Location', 'best');
grid on;
zdhc = min(hc);
annualreturn = (asset(end) / asset(1)) ^ (365 / days(date(end) - date(1))) - 1;
fprintf('最终资产为： %.2f\n', asset(end));
fprintf('年化收益率为： %.2f\n', annualreturn);
fprintf('最大回撤为： %.2f\n', zdhc);
fprintf('年度最差收益为： %.2f\n', ndhc);
fprintf('收益回撤比为： %.2f\n', -annualreturn / zdhc);
fprintf('近半年来收益率为： %.2f\n', asset(end) / asset(end - 120) - 1);
fprintf('日均轮换次数为： %.2f\n', mean(nt / 2));

function [pos_row, cash_out, n_trades] = rebalance_equal_weight( ...
    t, selected, pos_row, cash_in, B, buy_cost, sell_cost, n_trades)
% 仅对 selected 内标的等权调仓（先卖超配、再买欠配），不触碰池外持仓。
cash_out = cash_in;
pos_row = pos_row(:)';

if isempty(selected)
    return;
end

total = cash_out + sum(pos_row .* B(t, :));
target_mv = total / length(selected);

for j = 1:length(selected)
    idx = selected(j);
    cur_mv = pos_row(idx) * B(t, idx);
    if cur_mv > target_mv
        sell_mv = cur_mv - target_mv;
        pos_row(idx) = pos_row(idx) - sell_mv / B(t, idx);
        cash_out = cash_out + sell_mv * (1 - sell_cost);
        n_trades = n_trades + 1;
    end
end

for j = 1:length(selected)
    idx = selected(j);
    cur_mv = pos_row(idx) * B(t, idx);
    if cur_mv < target_mv - 1e-8
        buy_mv = target_mv - cur_mv;
        pos_row(idx) = pos_row(idx) + buy_mv / B(t, idx);
        cash_out = cash_out - buy_mv * (1 + buy_cost);
        n_trades = n_trades + 1;
    end
end
end
